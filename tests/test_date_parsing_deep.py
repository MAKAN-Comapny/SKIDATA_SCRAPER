"""
Comprehensive QA Test Suite: Date Parsing, Timezone Mathematics & Calendar Boundaries.
Covers 35+ test cases across date formats, leap years, month boundaries, and edge inputs.
"""

from datetime import date, datetime, timedelta, timezone
import unittest

import numpy as np
import pandas as pd

from validator import parse_flexible_date, get_ast_now, get_expected_yesterday, AST_TIMEZONE
from scraper import _parse_row_date


class TestDateParsingDeep(unittest.TestCase):
    # =========================================================================
    # Standard Date Formats (10 cases)
    # =========================================================================
    def test_parse_us_format(self):
        self.assertEqual(parse_flexible_date("11/14/2026"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("01/05/2026"), date(2026, 1, 5))
        self.assertEqual(parse_flexible_date("12/31/2026"), date(2026, 12, 31))

    def test_parse_iso_format(self):
        self.assertEqual(parse_flexible_date("2026-11-14"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("2026-02-28"), date(2026, 2, 28))
        self.assertEqual(parse_flexible_date("2026-01-01"), date(2026, 1, 1))

    def test_parse_excel_format(self):
        self.assertEqual(parse_flexible_date("14-Nov-26"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("14-Nov-2026"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("01-Jan-26"), date(2026, 1, 1))
        self.assertEqual(parse_flexible_date("31-Dec-26"), date(2026, 12, 31))

    # =========================================================================
    # Datetime and Timestamps with Time Components (8 cases)
    # =========================================================================
    def test_parse_datetime_strings_with_time(self):
        self.assertEqual(parse_flexible_date("2026-11-14 12:30:45"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("2026-11-14 23:59:59.999999"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("2026-11-14 00:00:00"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("11/14/2026 10:15:00"), date(2026, 11, 14))

    def test_parse_native_objects(self):
        self.assertEqual(parse_flexible_date(datetime(2026, 11, 14, 15, 30)), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date(date(2026, 11, 14)), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date(pd.Timestamp("2026-11-14 18:00:00")), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date(np.datetime64("2026-11-14T10:00:00")), date(2026, 11, 14))

    # =========================================================================
    # Leap Years & Calendar Boundary Transitions (8 cases)
    # =========================================================================
    def test_leap_year_february_29(self):
        # 2024 was leap year
        self.assertEqual(parse_flexible_date("02/29/2024"), date(2024, 2, 29))
        self.assertEqual(parse_flexible_date("2024-02-29"), date(2024, 2, 29))
        self.assertEqual(parse_flexible_date("29-Feb-24"), date(2024, 2, 29))

    def test_non_leap_year_february_28(self):
        # 2026 is non-leap year
        self.assertEqual(parse_flexible_date("02/28/2026"), date(2026, 2, 28))
        self.assertEqual(parse_flexible_date("28-Feb-26"), date(2026, 2, 28))

    def test_year_transition_boundary(self):
        # Dec 31 to Jan 1
        d_end = date(2026, 12, 31)
        d_start = d_end + timedelta(days=1)
        self.assertEqual(d_start, date(2027, 1, 1))
        self.assertEqual(parse_flexible_date("12/31/2026"), d_end)
        self.assertEqual(parse_flexible_date("01/01/2027"), d_start)

    # =========================================================================
    # Invalid, Null, Whitespace & Summary Values (10 cases)
    # =========================================================================
    def test_summary_totals_rows(self):
        self.assertIsNone(parse_flexible_date("Totals"))
        self.assertIsNone(parse_flexible_date("TOTALS"))
        self.assertIsNone(parse_flexible_date("totals"))
        self.assertIsNone(parse_flexible_date(" Total "))

    def test_null_none_nan(self):
        self.assertIsNone(parse_flexible_date(None))
        self.assertIsNone(parse_flexible_date(""))
        self.assertIsNone(parse_flexible_date("   "))
        self.assertIsNone(parse_flexible_date(float("nan")))
        self.assertIsNone(parse_flexible_date(pd.NA))
        self.assertIsNone(parse_flexible_date("invalid_date_string_abc"))

    # =========================================================================
    # Scraper _parse_row_date helper (4 cases)
    # =========================================================================
    def test_scraper_parse_row_date(self):
        self.assertEqual(_parse_row_date("11/14/2026"), date(2026, 11, 14))
        self.assertEqual(_parse_row_date(" 11/14/2026 (12,345) "), date(2026, 11, 14))
        self.assertIsNone(_parse_row_date("Totals"))
        self.assertIsNone(_parse_row_date(""))

    # =========================================================================
    # Timezone AST UTC+3 Precision (4 cases)
    # =========================================================================
    def test_ast_timezone_properties(self):
        now_ast = get_ast_now()
        yesterday_ast = get_expected_yesterday()
        self.assertEqual(now_ast.tzinfo, AST_TIMEZONE)
        self.assertEqual(now_ast.utcoffset(), timedelta(hours=3))
        self.assertEqual(yesterday_ast, (now_ast - timedelta(days=1)).date())
        # Ensure difference between now_ast date and yesterday_ast is exactly 1 day
        self.assertEqual((now_ast.date() - yesterday_ast).days, 1)


if __name__ == "__main__":
    unittest.main()
