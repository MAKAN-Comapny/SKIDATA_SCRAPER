"""
skidata_reader.py - Reader for SkiData Source Data (15-Day Rolling).

Extracts:
  1. High-level Daily Revenue & Ticket Counts from Revenue-In-Depth.xlsx
  2. Individual Ticket Transactions from Parking-Transactions.xlsx for discrepancy isolation
  3. Maps SkiData Car Park names to canonical Fabric DimMalls names
"""

from __future__ import annotations

import glob
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

logger = logging.getLogger("skidata_reader")

# Canonical mapping matching odoo_transformer.py and Fabric DimMalls
CAR_PARK_TO_MALL = {
    "Westfield DAMMAM": "02 - Nakheel Mall Dammam",
    "Westfield Dammam": "02 - Nakheel Mall Dammam",
    "View Mall": "01 - The View Mall Riyadh",
    "Alnoor Mall": "06 - Al Noor Mall Madinah",
    "Mall of Arabia": "03 - Arab Mall Jeddah",
    "Mall Of Arabia": "03 - Arab Mall Jeddah",
    "Premium Carpark": "06 - Al Noor Mall Madinah",
}


def normalize_mall_name(val: Any) -> str:
    """Normalizes car park / facility names to canonical Fabric DimMalls names (case-insensitive)."""
    if not val or pd.isna(val):
        return "Unknown"
    s = str(val).strip()
    if s in CAR_PARK_TO_MALL:
        return CAR_PARK_TO_MALL[s]
    s_clean = re.sub(r"[^a-zA-Z0-9]", "", s).lower()
    if "westfield" in s_clean or "nakheel" in s_clean:
        return "02 - Nakheel Mall Dammam"
    if "arabia" in s_clean or "arab" in s_clean:
        return "03 - Arab Mall Jeddah"
    if "view" in s_clean:
        return "01 - The View Mall Riyadh"
    if "noor" in s_clean or "premium" in s_clean or "alnoor" in s_clean:
        return "06 - Al Noor Mall Madinah"
    return s


class SkiDataReader:
    """Reads and parses raw/cleaned SkiData 15-day rolling files."""

    def __init__(self, exports_dir: str = "Cleaned-Exports", fallback_dir: str = "exports"):
        self.exports_dir = exports_dir
        self.fallback_dir = fallback_dir
        self._cached_parking_df: Optional[pd.DataFrame] = None
        self._cached_file_loaded: Optional[str] = None

    def _find_file(self, pattern: str) -> Optional[str]:
        """Looks for a file matching pattern in exports_dir, then fallback_dir."""
        for d in [self.exports_dir, self.fallback_dir]:
            matches = glob.glob(os.path.join(d, pattern))
            if matches:
                # Return newest file if multiple
                matches.sort(key=os.path.getmtime, reverse=True)
                return matches[0]
        return None

    def get_daily_revenue_summary(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Extracts daily gross revenue and ticket counts per mall from Revenue-In-Depth.xlsx.
        Very fast (< 1s) because Revenue-In-Depth contains daily totals.
        """
        revenue_file = self._find_file("*Revenue-In-Depth.xlsx")
        if not revenue_file:
            logger.warning("No Revenue-In-Depth file found. Generating synthetic source data for local testing.")
            return self._generate_mock_summary(start_date, end_date)

        logger.info(f"Reading SkiData revenue summary from: {revenue_file}")
        
        # Read header to determine header row offset (Qlik often uses row 0 or 1)
        raw_head = pd.read_excel(revenue_file, nrows=3)
        header_row = 1 if "Gross Revenue" not in raw_head.columns else 0
        df = pd.read_excel(revenue_file, header=header_row)

        # Standardize column names
        col_map = {
            "Car Park": "Car_Park",
            "Gross Revenue": "Gross_Revenue",
            "Quantity": "Tickets",
        }
        for old, new in col_map.items():
            for c in df.columns:
                if old.lower() == str(c).strip().lower():
                    df.rename(columns={c: new}, inplace=True)

        if "Date" not in df.columns or "Car_Park" not in df.columns:
            raise ValueError(f"Required columns missing in {revenue_file}. Found: {df.columns.tolist()}")

        # Clean Date
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
        df = df[df["Date"].notna()]
        
        # Filter to requested date window
        df = df[(df["Date"] >= start_date) & (df["Date"] <= end_date)]

        # Ensure numeric
        df["Gross_Revenue"] = pd.to_numeric(df["Gross_Revenue"], errors="coerce").fillna(0.0)
        df["Tickets"] = pd.to_numeric(df.get("Tickets", 1), errors="coerce").fillna(1)

        # Aggregate by Date and Car Park
        summary = (
            df.groupby(["Date", "Car_Park"], as_index=False)
            .agg({"Tickets": "sum", "Gross_Revenue": "sum"})
        )

        # Map to canonical Mall Names
        summary["Mall_Name"] = summary["Car_Park"].apply(normalize_mall_name)
        
        # Aggregate again in case multiple car parks mapped to same Mall
        final_summary = (
            summary.groupby(["Date", "Mall_Name"], as_index=False)
            .agg({"Tickets": "sum", "Gross_Revenue": "sum"})
            .rename(columns={"Tickets": "SkiData_Tickets", "Gross_Revenue": "SkiData_Revenue"})
        )
        return final_summary

    def get_ticket_details(self, target_date: date, mall_name: Optional[str] = None) -> pd.DataFrame:
        """
        Extracts specific ticket rows for a given date from Parking-Transactions.xlsx
        to compare against Fabric and isolate missing ticket IDs.
        Caches the parsed file in memory so multiple date lookups are instantaneous.
        """
        parking_file = self._find_file("*Parking-Transactions.xlsx")
        if not parking_file:
            logger.warning("No Parking-Transactions file found. Using mock tickets.")
            return self._generate_mock_tickets(target_date, mall_name)

        if self._cached_parking_df is None or self._cached_file_loaded != parking_file:
            logger.info(f"Loading and caching Parking-Transactions from: {parking_file} (one-time load)")
            raw_df = pd.read_excel(parking_file, dtype={"Serial Number": str, "Receipt Number": str})
            
            date_col = "Transaction Date/Time" if "Transaction Date/Time" in raw_df.columns else "Date"
            raw_df["_ReconDate"] = pd.to_datetime(raw_df[date_col], errors="coerce").dt.date
            
            car_park_col = "Car Park" if "Car Park" in raw_df.columns else "Facility"
            raw_df["_MallName"] = raw_df[car_park_col].apply(normalize_mall_name)

            res = pd.DataFrame()
            res["Ticket_Number"] = raw_df["Serial Number"].astype(str)
            res["Date"] = raw_df["_ReconDate"]
            res["In_Time"] = raw_df.get("Entry Date/Time", "")
            res["Out_Time"] = raw_df.get("Exit Date/Time", "")
            res["Payment"] = pd.to_numeric(raw_df.get("Payment", 0.0), errors="coerce").fillna(0.0)
            res["Payment_Device"] = raw_df.get("Payment Device", "")
            res["Mall_Name"] = raw_df["_MallName"]
            
            self._cached_parking_df = res
            self._cached_file_loaded = parking_file

        df = self._cached_parking_df
        filtered = df[df["Date"] == target_date].copy()
        if mall_name:
            filtered = filtered[filtered["Mall_Name"] == mall_name]
        return filtered

    def _generate_mock_summary(self, start_date: date, end_date: date) -> pd.DataFrame:
        """Generates mock summary for offline verification."""
        rows = []
        curr = start_date
        while curr <= end_date:
            for cp, mall in CAR_PARK_TO_MALL.items():
                if "Premium" in cp:
                    continue
                rows.append([curr, mall, 1200, 12000.0])
            curr += timedelta(days=1)
        return pd.DataFrame(rows, columns=["Date", "Mall_Name", "SkiData_Tickets", "SkiData_Revenue"])

    def _generate_mock_tickets(self, target_date: date, mall_name: Optional[str] = None) -> pd.DataFrame:
        rows = []
        target_mall = mall_name or "03 - Arab Mall Jeddah"
        for i in range(1, 105):
            ticket_id = f"331719841050293000{i:05d}"
            rows.append([ticket_id, target_date, "2026-11-20 21:00:00", "2026-11-20 23:30:00", 10.0, "EX1-Z1", target_mall])
        return pd.DataFrame(rows, columns=["Ticket_Number", "Date", "In_Time", "Out_Time", "Payment", "Payment_Device", "Mall_Name"])
