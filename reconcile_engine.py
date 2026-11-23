"""
reconcile_engine.py - Core Reconciliation Engine for SkiData vs. Microsoft Fabric.

Performs:
  1. High-level Outer Join on [Date, Mall_Name]
  2. Variance calculation (Count Diff, Gross Revenue Diff, Status)
  3. Drill-down comparison on mismatched days to extract:
     - Tickets present in SkiData, MISSING in Fabric
     - Tickets present in Fabric, MISSING in SkiData
  4. Generation of a multi-tab Excel audit workbook
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from fabric_reader import FabricReader
from skidata_reader import SkiDataReader

logger = logging.getLogger("reconcile_engine")


@dataclass
class ReconciliationResult:
    start_date: date
    end_date: date
    execution_time: datetime
    summary_df: pd.DataFrame
    mismatches_df: pd.DataFrame
    missing_in_fabric: Dict[str, pd.DataFrame] = field(default_factory=dict)
    missing_in_skidata: Dict[str, pd.DataFrame] = field(default_factory=dict)
    excel_report_path: Optional[str] = None
    total_mismatches: int = 0
    total_revenue_diff: float = 0.0


class ReconcileEngine:
    """Orchestrates comparison between SkiData source and Fabric UnifiedWarehouse."""

    def __init__(self, fabric_reader: FabricReader, skidata_reader: SkiDataReader):
        self.fabric = fabric_reader
        self.skidata = skidata_reader

    def run_reconciliation(
        self,
        days: int = 15,
        target_end_date: Optional[date] = None,
        output_dir: str = "reconciliation_reports",
    ) -> ReconciliationResult:
        """Executes full 15-day reconciliation and returns structured findings."""
        end_d = target_end_date or (datetime.now().date() - timedelta(days=1))
        start_d = end_d - timedelta(days=days - 1)
        exec_time = datetime.now()

        logger.info(f"Starting 15-day reconciliation: {start_d} to {end_d}")

        # 1. Fetch Summaries
        skidata_summary = self.skidata.get_daily_revenue_summary(start_d, end_d)
        fabric_summary = self.fabric.get_daily_revenue_summary(start_d, end_d)

        # 2. Outer Join on [Date, Mall_Name]
        merged = pd.merge(
            skidata_summary,
            fabric_summary,
            on=["Date", "Mall_Name"],
            how="outer",
        )

        # Fill NaNs with 0
        merged["SkiData_Tickets"] = merged["SkiData_Tickets"].fillna(0).astype(int)
        merged["Fabric_Tickets"] = merged["Fabric_Tickets"].fillna(0).astype(int)
        merged["SkiData_Revenue"] = merged["SkiData_Revenue"].fillna(0.0).astype(float)
        merged["Fabric_Revenue"] = merged["Fabric_Revenue"].fillna(0.0).astype(float)

        # 3. Calculate Variances
        merged["Diff_Tickets"] = merged["SkiData_Tickets"] - merged["Fabric_Tickets"]
        merged["Diff_Revenue"] = merged["SkiData_Revenue"] - merged["Fabric_Revenue"]

        # Classify Status
        def _classify(row):
            if row["SkiData_Tickets"] > 0 and row["Fabric_Tickets"] == 0:
                return "MISSING_IN_FABRIC"
            if row["SkiData_Tickets"] == 0 and row["Fabric_Tickets"] > 0:
                return "MISSING_IN_SKIDATA"
            if row["Diff_Tickets"] == 0 and abs(row["Diff_Revenue"]) < 1.0:
                return "MATCH"
            return "MISMATCH"

        merged["Status"] = merged.apply(_classify, axis=1)
        merged = merged.sort_values(by=["Date", "Mall_Name"], ascending=[False, True]).reset_index(drop=True)

        mismatches = merged[merged["Status"] != "MATCH"].copy()
        total_mismatches = len(mismatches)
        total_rev_diff = float(mismatches["Diff_Revenue"].sum()) if not mismatches.empty else 0.0

        logger.info(f"Reconciliation matrix complete. Evaluated {len(merged)} rows. Mismatches found: {total_mismatches}")

        # 4. Drill-Down on Inconsistencies (Isolate specific missing tickets)
        missing_in_fabric_dict: Dict[str, pd.DataFrame] = {}
        missing_in_skidata_dict: Dict[str, pd.DataFrame] = {}

        if not mismatches.empty:
            logger.info("Drilling down into mismatched dates/malls to isolate missing ticket IDs...")
            # Query Fabric ONCE per distinct date and cache in memory (avoids 75x redundant roundtrips)
            fabric_tickets_by_date: Dict[date, pd.DataFrame] = {}
            for d in mismatches["Date"].unique():
                try:
                    fabric_tickets_by_date[d] = self.fabric.get_ticket_details(d)
                except Exception as ef:
                    logger.warning(f"Could not load ticket details for {d} from Fabric: {ef}")
                    fabric_tickets_by_date[d] = pd.DataFrame()

            for _, row in mismatches.iterrows():
                row_date = row["Date"]
                row_mall = row["Mall_Name"]
                key = f"{row_date} | {row_mall}"

                skidata_tickets = self.skidata.get_ticket_details(row_date, row_mall)
                all_fab_tickets = fabric_tickets_by_date.get(row_date, pd.DataFrame())

                # Filter Fabric tickets for this specific mall
                if not all_fab_tickets.empty and "Mall_Name" in all_fab_tickets.columns:
                    fab_for_mall = all_fab_tickets[all_fab_tickets["Mall_Name"] == row_mall]
                    # If location name mapping differed, use all_fab_tickets as fallback
                    if fab_for_mall.empty and len(all_fab_tickets) > 0:
                        fab_for_mall = all_fab_tickets
                else:
                    fab_for_mall = all_fab_tickets

                skidata_set = (
                    set(skidata_tickets["Ticket_Number"].astype(str).str.strip())
                    if not skidata_tickets.empty else set()
                )
                fabric_set = (
                    set(fab_for_mall["Ticket_Number"].astype(str).str.strip())
                    if not fab_for_mall.empty else set()
                )

                # A: In SkiData, Missing in Fabric
                diff_a = skidata_set - fabric_set
                if diff_a and not skidata_tickets.empty:
                    missing_fab_df = skidata_tickets[
                        skidata_tickets["Ticket_Number"].astype(str).isin(diff_a)
                    ].copy()
                    missing_in_fabric_dict[key] = missing_fab_df

                # B: In Fabric, Missing in SkiData
                # Ensure we only compare mall-specific tickets to avoid cross-mall false positives
                diff_b = fabric_set - skidata_set
                if diff_b and not fab_for_mall.empty:
                    if "Mall_Name" in fab_for_mall.columns and (fab_for_mall["Mall_Name"] == row_mall).any():
                        matched_fab = fab_for_mall[fab_for_mall["Mall_Name"] == row_mall]
                        diff_b = set(matched_fab["Ticket_Number"].astype(str).str.strip()) - skidata_set
                        fab_for_mall = matched_fab

                    missing_ski_df = fab_for_mall[
                        fab_for_mall["Ticket_Number"].astype(str).isin(diff_b)
                    ].copy()
                    missing_in_skidata_dict[key] = missing_ski_df

        # 5. Export to Single Executive Excel File
        os.makedirs(output_dir, exist_ok=True)
        report_filename = f"SkiData_Fabric_Reconciliation_{end_d.isoformat()}.xlsx"
        report_path = os.path.join(output_dir, report_filename)

        _build_executive_excel(
            report_path=report_path,
            merged=merged,
            mismatches=mismatches,
            missing_in_fabric_dict=missing_in_fabric_dict,
            missing_in_skidata_dict=missing_in_skidata_dict,
            start_d=start_d,
            end_d=end_d,
            exec_time=exec_time,
            total_mismatches=total_mismatches,
            total_rev_diff=total_rev_diff,
        )

        logger.info(f"Reconciliation Excel audit report saved: {report_path}")

        return ReconciliationResult(
            start_date=start_d,
            end_date=end_d,
            execution_time=exec_time,
            summary_df=merged,
            mismatches_df=mismatches,
            missing_in_fabric=missing_in_fabric_dict,
            missing_in_skidata=missing_in_skidata_dict,
            excel_report_path=report_path,
            total_mismatches=total_mismatches,
            total_revenue_diff=total_rev_diff,
        )


def _build_executive_excel(
    report_path: str,
    merged: pd.DataFrame,
    mismatches: pd.DataFrame,
    missing_in_fabric_dict: Dict[str, pd.DataFrame],
    missing_in_skidata_dict: Dict[str, pd.DataFrame],
    start_d: date,
    end_d: date,
    exec_time: datetime,
    total_mismatches: int,
    total_rev_diff: float,
) -> None:
    """
    Builds a single, executive-formatted Excel workbook.
    Tab 1: Actionable_Discrepancies (Visual dashboard showing ONLY missing/mismatched data)
    Tab 2: All_Dates_15Day_Matrix (Complete master summary of all pairs)
    Tab 3: Missing_In_Fabric (Detailed ticket sequences missing in Fabric)
    Tab 4: Missing_In_SkiData (Detailed ticket sequences extra in Fabric)
    """
    wb = openpyxl.Workbook()

    font_title = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    font_sub = Font(name="Calibri", size=10, italic=True, color="555555")
    font_section = Font(name="Calibri", size=12, bold=True, color="1A237E")
    font_th = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_body = Font(name="Calibri", size=11)
    font_kpi_label = Font(name="Calibri", size=9, bold=True, color="666666")

    fill_header = PatternFill(start_color="1A237E", end_color="1A237E", fill_type="solid")
    fill_th = PatternFill(start_color="283593", end_color="283593", fill_type="solid")
    fill_kpi = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")
    fill_match = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
    fill_missing_fab = PatternFill(start_color="FFEBEE", end_color="FFEBEE", fill_type="solid")
    fill_missing_ski = PatternFill(start_color="F3E5F5", end_color="F3E5F5", fill_type="solid")
    fill_mismatch = PatternFill(start_color="FFF3E0", end_color="FFF3E0", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0"),
    )

    # -------------------------------------------------------------
    # TAB 1: Actionable_Discrepancies (The Executive Dashboard Sheet)
    # -------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "Actionable_Discrepancies"
    ws1.views.sheetView[0].showGridLines = True

    # Title Banner
    ws1.merge_cells("A1:I1")
    ws1["A1"] = "MAKAN PARKING: 15-DAY AUDIT & DISCREPANCY DASHBOARD"
    ws1["A1"].font = font_title
    ws1["A1"].fill = fill_header
    ws1["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws1.row_dimensions[1].height = 35

    ws1["A2"] = f"Reconciliation Window: {start_d} to {end_d} | Generated: {exec_time.strftime('%d-%b-%Y %I:%M %p AST')}"
    ws1["A2"].font = font_sub

    # KPI Summary Cards (Row 4 to 5)
    ws1["A4"] = "OVERALL STATUS"
    ws1["A4"].font = font_kpi_label
    ws1["A5"] = "100% IN SYNC (ALL MATCH)" if total_mismatches == 0 else f"{total_mismatches} INCONSISTENCIES FOUND"
    ws1["A5"].font = Font(name="Calibri", size=13, bold=True, color="2E7D32" if total_mismatches == 0 else "C62828")

    ws1["D4"] = "TOTAL DISCREPANCIES"
    ws1["D4"].font = font_kpi_label
    ws1["D5"] = f"{total_mismatches} Pairs"
    ws1["D5"].font = Font(name="Calibri", size=13, bold=True, color="1A237E")

    ws1["G4"] = "NET REVENUE VARIANCE"
    ws1["G4"].font = font_kpi_label
    ws1["G5"] = f"{total_rev_diff:+,.2f} SAR"
    ws1["G5"].font = Font(name="Calibri", size=13, bold=True, color="C62828" if abs(total_rev_diff) > 0.01 else "2E7D32")

    for col in ["A", "B", "C", "D", "E", "F", "G", "H", "I"]:
        for r in [4, 5]:
            ws1[f"{col}{r}"].fill = fill_kpi

    # Discrepancy Table Section
    ws1["A7"] = "🚨 ACTION REQUIRED: SPECIFIC DATES & FACILITIES TO RE-RUN IN FABRIC"
    ws1["A7"].font = font_section

    headers1 = [
        "Date", "Mall Name", "SkiData Qty", "Fabric Qty", "Diff (Qty)",
        "SkiData (SAR)", "Fabric (SAR)", "Diff (SAR)", "Recommended Action"
    ]
    for c_idx, h in enumerate(headers1, start=1):
        cell = ws1.cell(row=8, column=c_idx, value=h)
        cell.font = font_th
        cell.fill = fill_th
        cell.alignment = Alignment(horizontal="center" if "Date" in h or "Action" in h else "right", vertical="center")

    curr_row = 9
    if mismatches.empty:
        ws1.merge_cells("A9:I9")
        cell_empty = ws1["A9"]
        cell_empty.value = "🎉 All dates and facilities are in 100% sync! No missing data or re-run needed."
        cell_empty.font = Font(name="Calibri", size=12, bold=True, color="2E7D32")
        cell_empty.fill = fill_match
        cell_empty.alignment = Alignment(horizontal="center", vertical="center")
        ws1.row_dimensions[9].height = 30
        curr_row = 11
    else:
        for _, row in mismatches.iterrows():
            status = row["Status"]
            if status == "MISSING_IN_FABRIC":
                action_text = f"⚠️ MISSING IN FABRIC: Re-run ETL for {row['Date']}"
                row_fill = fill_missing_fab
            elif status == "MISSING_IN_SKIDATA":
                action_text = f"ℹ️ EXTRA IN FABRIC: Check duplicate ingestion for {row['Date']}"
                row_fill = fill_missing_ski
            else:
                action_text = f"⚠️ RECON VARIANCE: Audit {row['Date']} {row['Mall_Name']}"
                row_fill = fill_mismatch

            vals = [
                str(row["Date"]),
                str(row["Mall_Name"]),
                int(row["SkiData_Tickets"]),
                int(row["Fabric_Tickets"]),
                int(row["Diff_Tickets"]),
                float(row["SkiData_Revenue"]),
                float(row["Fabric_Revenue"]),
                float(row["Diff_Revenue"]),
                action_text,
            ]
            for c_idx, val in enumerate(vals, start=1):
                cell = ws1.cell(row=curr_row, column=c_idx, value=val)
                cell.font = font_body
                cell.fill = row_fill
                cell.border = thin_border
                if c_idx in (3, 4, 5):
                    cell.number_format = "#,##0"
                    cell.alignment = Alignment(horizontal="right")
                elif c_idx in (6, 7, 8):
                    cell.number_format = "#,##0.00"
                    cell.alignment = Alignment(horizontal="right")
                elif c_idx == 1:
                    cell.alignment = Alignment(horizontal="center")
            curr_row += 1

    # Auto-fit columns for Tab 1
    for col in ws1.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws1.column_dimensions[col_letter].width = max(max_len + 3, 14)
    ws1.column_dimensions["I"].width = 45

    # -------------------------------------------------------------
    # TAB 2: All_Dates_15Day_Matrix (Complete Master Reference)
    # -------------------------------------------------------------
    ws2 = wb.create_sheet(title="All_Dates_15Day_Matrix")
    ws2.views.sheetView[0].showGridLines = True

    headers2 = ["Date", "Mall_Name", "SkiData_Tickets", "Fabric_Tickets", "Diff_Tickets", "SkiData_Revenue", "Fabric_Revenue", "Diff_Revenue", "Status"]
    for c_idx, h in enumerate(headers2, start=1):
        cell = ws2.cell(row=1, column=c_idx, value=h)
        cell.font = font_th
        cell.fill = fill_th
        cell.alignment = Alignment(horizontal="center" if "Date" in h or "Status" in h or "Name" in h else "right")

    for r_idx, row in enumerate(merged.itertuples(index=False), start=2):
        for c_idx, val in enumerate(row, start=1):
            cell = ws2.cell(row=r_idx, column=c_idx, value=val)
            cell.font = font_body
            cell.border = thin_border
            if c_idx in (3, 4, 5):
                cell.number_format = "#,##0"
                cell.alignment = Alignment(horizontal="right")
            elif c_idx in (6, 7, 8):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")

    for col in ws2.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws2.column_dimensions[col_letter].width = max(max_len + 3, 14)

    # -------------------------------------------------------------
    # TAB 3: Missing_In_Fabric (Detailed Tickets)
    # -------------------------------------------------------------
    ws3 = wb.create_sheet(title="Missing_In_Fabric")
    all_missing_fab = [df for df in missing_in_fabric_dict.values() if not df.empty]
    if all_missing_fab:
        comb_fab = pd.concat(all_missing_fab, ignore_index=True)
        if "Ticket_Number" in comb_fab.columns:
            comb_fab = comb_fab.drop_duplicates(subset=["Ticket_Number"])
        if len(comb_fab) > 1000000:
            comb_fab = comb_fab.iloc[:1000000]

        for c_idx, col_name in enumerate(comb_fab.columns, start=1):
            cell = ws3.cell(row=1, column=c_idx, value=col_name)
            cell.font = font_th
            cell.fill = fill_th
        for r_idx, row_vals in enumerate(comb_fab.itertuples(index=False), start=2):
            for c_idx, v in enumerate(row_vals, start=1):
                ws3.cell(row=r_idx, column=c_idx, value=str(v) if v is not None else "")
    else:
        ws3["A1"] = "No tickets missing in Fabric."

    # -------------------------------------------------------------
    # TAB 4: Missing_In_SkiData (Detailed Tickets)
    # -------------------------------------------------------------
    ws4 = wb.create_sheet(title="Missing_In_SkiData")
    all_missing_ski = [df for df in missing_in_skidata_dict.values() if not df.empty]
    if all_missing_ski:
        comb_ski = pd.concat(all_missing_ski, ignore_index=True)
        if "Ticket_Number" in comb_ski.columns:
            comb_ski = comb_ski.drop_duplicates(subset=["Ticket_Number"])
        if len(comb_ski) > 1000000:
            comb_ski = comb_ski.iloc[:1000000]

        for c_idx, col_name in enumerate(comb_ski.columns, start=1):
            cell = ws4.cell(row=1, column=c_idx, value=col_name)
            cell.font = font_th
            cell.fill = fill_th
        for r_idx, row_vals in enumerate(comb_ski.itertuples(index=False), start=2):
            for c_idx, v in enumerate(row_vals, start=1):
                ws4.cell(row=r_idx, column=c_idx, value=str(v) if v is not None else "")
    else:
        ws4["A1"] = "No extra tickets found in Fabric."

    wb.save(report_path)
