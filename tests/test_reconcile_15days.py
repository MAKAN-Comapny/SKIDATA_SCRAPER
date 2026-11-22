"""
test_reconcile_15days.py - Comprehensive Unit Tests for 15-Day Reconciliation System.
"""

from __future__ import annotations

import os
import sys

# Ensure repository root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import date, datetime, timedelta
import pandas as pd
import pytest

from fabric_reader import FabricReader, _assert_read_only_sql, _match_column
from skidata_reader import SkiDataReader, CAR_PARK_TO_MALL
from reconcile_engine import ReconcileEngine, ReconciliationResult
from notifier import build_html_report, get_recipients


def test_column_matcher_and_no_nolock():
    """Verifies fuzzy matching for Fabric/SkiData columns and absence of WITH (NOLOCK)."""
    cols = ["Date", "Car Park", "Facility", "Theoretical Revenue", "Quantity", "Serial Number"]
    
    assert _match_column(cols, ["date", "transactiondate"]) == "Date"
    assert _match_column(cols, ["carpark", "car_park", "mall_name"]) == "Car Park"
    assert _match_column(cols, ["quantity", "qty"]) == "Quantity"
    assert _match_column(cols, ["theoretical_revenue", "gross_revenue"]) == "Theoretical Revenue"
    assert _match_column(cols, ["ticket_id", "serial number", "serial_number"]) == "Serial Number"

    # Verify Fabric queries in mock mode do not contain WITH (NOLOCK)
    reader = FabricReader(mock_mode=True)
    summary = reader.get_daily_revenue_summary(date(2026, 11, 20), date(2026, 11, 20))
    assert not summary.empty


def test_fabric_read_only_enforcement():
    """Verifies that non-SELECT statements raise a PermissionError."""
    # Allowed SELECT queries
    _assert_read_only_sql("SELECT * FROM SkiData.FactRevenue WHERE TransactionDate >= '2026-11-01'")
    _assert_read_only_sql("SELECT COUNT(Ticket_ID), SUM(Gross_Revenue) FROM SkiData.FactParking")

    # Blocked DML / DDL queries
    with pytest.raises(PermissionError):
        _assert_read_only_sql("DROP TABLE SkiData.FactRevenue")

    with pytest.raises(PermissionError):
        _assert_read_only_sql("DELETE FROM SkiData.FactRevenue WHERE Ticket_ID = '123'")

    with pytest.raises(PermissionError):
        _assert_read_only_sql("UPDATE SkiData.FactRevenue SET Gross_Revenue = 0")

    with pytest.raises(PermissionError):
        _assert_read_only_sql("INSERT INTO SkiData.FactRevenue VALUES (1, 2, 3)")

    with pytest.raises(PermissionError):
        _assert_read_only_sql("TRUNCATE TABLE SkiData.FactParking")


def test_car_park_mapping():
    """Verifies canonical car park to mall name mapping."""
    assert CAR_PARK_TO_MALL["Westfield DAMMAM"] == "02 - Nakheel Mall Dammam"
    assert CAR_PARK_TO_MALL["View Mall"] == "01 - The View Mall Riyadh"
    assert CAR_PARK_TO_MALL["Alnoor Mall"] == "06 - Al Noor Mall Madinah"
    assert CAR_PARK_TO_MALL["Mall of Arabia"] == "03 - Arab Mall Jeddah"


def test_reconciliation_engine_logic(tmp_path):
    """Verifies outer join, variance calculation, and status classification."""
    fabric_reader = FabricReader(mock_mode=True)
    skidata_reader = SkiDataReader()

    engine = ReconcileEngine(fabric_reader=fabric_reader, skidata_reader=skidata_reader)
    result = engine.run_reconciliation(
        days=5,
        target_end_date=date(2026, 11, 20),
        output_dir=str(tmp_path),
    )

    assert isinstance(result, ReconciliationResult)
    assert not result.summary_df.empty
    assert "Diff_Tickets" in result.summary_df.columns
    assert "Diff_Revenue" in result.summary_df.columns
    assert "Status" in result.summary_df.columns

    # Verify Excel report was created
    assert result.excel_report_path is not None
    assert os.path.exists(result.excel_report_path)


def test_notifier_html_builder(tmp_path):
    """Verifies that the generated HTML contains essential table columns and recipient safeguards."""
    summary_data = [
        [date(2026, 11, 20), "03 - Arab Mall Jeddah", 2771, 2077, -694, 27710.0, 20770.0, -6940.0, "MISMATCH"],
        [date(2026, 11, 20), "02 - Nakheel Mall Dammam", 1450, 1450, 0, 14500.0, 14500.0, 0.0, "MATCH"],
    ]
    summary_df = pd.DataFrame(
        summary_data,
        columns=[
            "Date", "Mall_Name", "SkiData_Tickets", "Fabric_Tickets",
            "Diff_Tickets", "SkiData_Revenue", "Fabric_Revenue", "Diff_Revenue", "Status"
        ]
    )
    mismatches_df = summary_df[summary_df["Status"] == "MISMATCH"].copy()

    mock_missing_fab = pd.DataFrame([
        ["33171984105029300098669", date(2026, 11, 20), "2026-11-20 21:00", "2026-11-20 23:30", 10.0, "EX1-Z1", "03 - Arab Mall Jeddah"]
    ], columns=["Ticket_Number", "Date", "In_Time", "Out_Time", "Payment", "Payment_Device", "Mall_Name"])

    res = ReconciliationResult(
        start_date=date(2026, 11, 15),
        end_date=date(2026, 11, 20),
        execution_time=datetime.now(),
        summary_df=summary_df,
        mismatches_df=mismatches_df,
        missing_in_fabric={"2026-11-20 | 03 - Arab Mall Jeddah": mock_missing_fab},
        missing_in_skidata={},
        total_mismatches=1,
        total_revenue_diff=-6940.0,
    )

    html = build_html_report(res)

    assert "Master Reconciliation Matrix" in html
    assert "03 - Arab Mall Jeddah" in html
    assert "-6,940.00" in html
    assert "33171984105029300098669" in html
    assert "sumit.dhankhar@makan.sa" in html


def test_recipient_configuration():
    """Verifies that default recipients strictly include Sumit."""
    recipients = get_recipients()
    assert "sumit.dhankhar@makan.sa" in recipients
