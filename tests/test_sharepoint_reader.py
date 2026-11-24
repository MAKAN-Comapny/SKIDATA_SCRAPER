"""
test_sharepoint_reader.py - Unit tests for SharePoint Reader and Stale Data Guard.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

# Ensure repository root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from unittest.mock import patch, MagicMock

from sharepoint_reader import (
    parse_date_from_filename,
    download_latest_from_sharepoint,
    get_ast_now,
    AST_TIMEZONE,
)


def test_parse_date_from_filename():
    """Verifies date extraction across various SkiData file naming patterns."""
    assert parse_date_from_filename("24-Nov-26-Parking-Transactions.xlsx") == date(2026, 11, 24)
    assert parse_date_from_filename("24-Nov-26-12Noon-Parking-Transactions.xlsx") == date(2026, 11, 24)
    assert parse_date_from_filename("24-Nov-26-Morning-Revenue-In-Depth.xlsx") == date(2026, 11, 24)
    assert parse_date_from_filename("10-Nov-26-To-24-Nov-26-Parking-Transactions.xlsx") == date(2026, 11, 24)
    assert parse_date_from_filename("2026-11-24-Parking-Transactions.xlsx") == date(2026, 11, 24)
    assert parse_date_from_filename("24-November-2026-Parking-Transactions.xlsx") == date(2026, 11, 24)
    assert parse_date_from_filename("invalid_filename.xlsx") is None


def test_stale_data_guard_blocks_old_files(tmp_path):
    """
    Verifies that if today's data is NOT fetched, the Stale Data Guard
    aborts execution instead of reconciling on stale/old data.
    """
    today_ast = get_ast_now().date()
    stale_date = today_ast - timedelta(days=5)
    stale_date_str = stale_date.strftime("%d-%b-%y")

    mock_items = [
        {
            "name": f"{stale_date_str}-Parking-Transactions.xlsx",
            "file": {},
            "size": 1024,
            "lastModifiedDateTime": f"{stale_date.isoformat()}T10:00:00Z",
            "@microsoft.graph.downloadUrl": "https://example.com/dl/parking",
        },
        {
            "name": f"{stale_date_str}-Revenue-In-Depth.xlsx",
            "file": {},
            "size": 1024,
            "lastModifiedDateTime": f"{stale_date.isoformat()}T10:00:00Z",
            "@microsoft.graph.downloadUrl": "https://example.com/dl/revenue",
        },
    ]

    with patch("sharepoint_reader.list_sharepoint_folder", return_value=mock_items):
        with pytest.raises(ValueError) as exc_info:
            download_latest_from_sharepoint(
                source_folder="Ski Data 5",
                target_date=None,
                allow_stale=False,
                dest_dir=str(tmp_path),
            )
        assert "STALE DATA GUARD" in str(exc_info.value)
        assert "Today's data has NOT been fetched yet" in str(exc_info.value)


def test_stale_data_guard_allows_fresh_data(tmp_path):
    """
    Verifies that fresh files (today or yesterday AST) pass the guard.
    """
    today_ast = get_ast_now().date()
    today_date_str = today_ast.strftime("%d-%b-%y")

    mock_items = [
        {
            "name": f"{today_date_str}-Parking-Transactions.xlsx",
            "file": {},
            "size": 1024,
            "lastModifiedDateTime": f"{today_ast.isoformat()}T10:00:00Z",
            "@microsoft.graph.downloadUrl": "https://example.com/dl/parking",
        },
        {
            "name": f"{today_date_str}-Revenue-In-Depth.xlsx",
            "file": {},
            "size": 1024,
            "lastModifiedDateTime": f"{today_ast.isoformat()}T10:00:00Z",
            "@microsoft.graph.downloadUrl": "https://example.com/dl/revenue",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.content = b"PK\x03\x04mock_excel_content"
    mock_resp.raise_for_status = MagicMock()

    with patch("sharepoint_reader.list_sharepoint_folder", return_value=mock_items), \
         patch("requests.get", return_value=mock_resp), \
         patch("Transformer.run", return_value=None):

        result = download_latest_from_sharepoint(
            source_folder="Ski Data 5",
            target_date=None,
            allow_stale=False,
            dest_dir=str(tmp_path),
        )

        assert "parking" in result
        assert "revenue" in result
        assert result["file_date"] == today_ast
        assert os.path.exists(result["parking"])
        assert os.path.exists(result["revenue"])


def test_allow_stale_flag_bypasses_guard(tmp_path):
    """
    Verifies that --allow-stale lets manual runs evaluate historical/older files.
    """
    stale_date = date(2026, 10, 1)
    stale_date_str = stale_date.strftime("%d-%b-%y")

    mock_items = [
        {
            "name": f"{stale_date_str}-Parking-Transactions.xlsx",
            "file": {},
            "size": 1024,
            "lastModifiedDateTime": "2026-10-01T10:00:00Z",
            "@microsoft.graph.downloadUrl": "https://example.com/dl/parking",
        },
        {
            "name": f"{stale_date_str}-Revenue-In-Depth.xlsx",
            "file": {},
            "size": 1024,
            "lastModifiedDateTime": "2026-10-01T10:00:00Z",
            "@microsoft.graph.downloadUrl": "https://example.com/dl/revenue",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.content = b"PK\x03\x04mock_excel_content"
    mock_resp.raise_for_status = MagicMock()

    with patch("sharepoint_reader.list_sharepoint_folder", return_value=mock_items), \
         patch("requests.get", return_value=mock_resp), \
         patch("Transformer.run", return_value=None):

        result = download_latest_from_sharepoint(
            source_folder="Ski Data 5",
            target_date=None,
            allow_stale=True,
            dest_dir=str(tmp_path),
        )

        assert result["file_date"] == stale_date
