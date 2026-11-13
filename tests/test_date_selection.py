"""Unit tests for adaptive date selection in scraper.py on main."""

import os
import sys
import unittest
from datetime import date, timedelta, datetime
from unittest.mock import patch, AsyncMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scraper import select_report_date


class _FakeDateItem:
    def __init__(self, text: str):
        self._text = text
        self.clicked = False

    async def inner_text(self) -> str:
        return self._text

    async def query_selector(self, selector: str):
        return None

    async def click(self):
        self.clicked = True


class _FakeListFrame:
    def __init__(self, mapping: dict[str, str]):
        self._items = {n: _FakeDateItem(txt) for n, txt in mapping.items()}

    async def query_selector(self, selector: str):
        for n_str, item in self._items.items():
            if f"data-n='{n_str}'" in selector:
                return item
        return None

    async def query_selector_all(self, selector: str):
        return list(self._items.values())


class _FakeKeyboard:
    async def press(self, key: str):
        pass


class _FakePage:
    def __init__(self):
        self.frames = []
        self.keyboard = _FakeKeyboard()

    async def query_selector_all(self, selector: str):
        return []

    async def query_selector(self, selector: str):
        return None


class TestDateSelection(unittest.IsolatedAsyncioTestCase):
    @patch("scraper.find_frame_with_selector", new_callable=AsyncMock)
    async def test_selects_row_0_when_row_0_is_yesterday(self, mock_find_frame):
        mock_find_frame.return_value = None
        expected_yesterday = (datetime.now() - timedelta(days=1)).date()
        yesterday_str = expected_yesterday.strftime("%m/%d/%Y")
        day_before = (expected_yesterday - timedelta(days=1)).strftime("%m/%d/%Y")

        frame = _FakeListFrame({"0": yesterday_str, "1": day_before})
        page = _FakePage()

        selected = await select_report_date(frame, "div.RowColumn-barContainer", page, confirm_required=False)
        self.assertEqual(selected, expected_yesterday)
        self.assertTrue(frame._items["0"].clicked)
        self.assertFalse(frame._items["1"].clicked)

    @patch("scraper.find_frame_with_selector", new_callable=AsyncMock)
    async def test_selects_row_1_when_row_0_is_today(self, mock_find_frame):
        mock_find_frame.return_value = None
        expected_today = datetime.now().date()
        expected_yesterday = (datetime.now() - timedelta(days=1)).date()
        today_str = expected_today.strftime("%m/%d/%Y")
        yesterday_str = expected_yesterday.strftime("%m/%d/%Y")

        frame = _FakeListFrame({"0": today_str, "1": yesterday_str})
        page = _FakePage()

        selected = await select_report_date(frame, "div.RowColumn-barContainer", page, confirm_required=False)
        self.assertEqual(selected, expected_yesterday)
        self.assertFalse(frame._items["0"].clicked)
        self.assertTrue(frame._items["1"].clicked)


if __name__ == "__main__":
    unittest.main()
