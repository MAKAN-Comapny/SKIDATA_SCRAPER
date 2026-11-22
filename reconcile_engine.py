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

        # 5. Export to Excel
        os.makedirs(output_dir, exist_ok=True)
        report_filename = f"SkiData_Fabric_Reconciliation_{end_d.isoformat()}.xlsx"
        report_path = os.path.join(output_dir, report_filename)

        with pd.ExcelWriter(report_path, engine="openpyxl") as writer:
            merged.to_excel(writer, sheet_name="Master_Summary", index=False)
            
            # Combine all missing in fabric rows (deduplicated & capped to Excel sheet 1,048,576 limit)
            all_missing_fab = [df for df in missing_in_fabric_dict.values() if not df.empty]
            if all_missing_fab:
                comb_fab = pd.concat(all_missing_fab, ignore_index=True)
                if "Ticket_Number" in comb_fab.columns:
                    comb_fab = comb_fab.drop_duplicates(subset=["Ticket_Number"])
                if len(comb_fab) > 1000000:
                    logger.warning(f"Capping Missing_In_Fabric from {len(comb_fab)} to 1,000,000 rows (Excel sheet row limit).")
                    comb_fab = comb_fab.iloc[:1000000]
                comb_fab.to_excel(writer, sheet_name="Missing_In_Fabric", index=False)
            else:
                pd.DataFrame({"Note": ["No tickets missing in Fabric."]}).to_excel(writer, sheet_name="Missing_In_Fabric", index=False)

            # Combine all missing in skidata rows (deduplicated & capped to Excel sheet 1,048,576 limit)
            all_missing_ski = [df for df in missing_in_skidata_dict.values() if not df.empty]
            if all_missing_ski:
                comb_ski = pd.concat(all_missing_ski, ignore_index=True)
                if "Ticket_Number" in comb_ski.columns:
                    comb_ski = comb_ski.drop_duplicates(subset=["Ticket_Number"])
                if len(comb_ski) > 1000000:
                    logger.warning(f"Capping Missing_In_SkiData from {len(comb_ski)} to 1,000,000 rows (Excel sheet row limit).")
                    comb_ski = comb_ski.iloc[:1000000]
                comb_ski.to_excel(writer, sheet_name="Missing_In_SkiData", index=False)
            else:
                pd.DataFrame({"Note": ["No extra tickets found in Fabric."]}).to_excel(writer, sheet_name="Missing_In_SkiData", index=False)

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
