"""Unit tests for adaptive date selection and report retries in scraper.py on main."""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scraper import (
    resolve_target_start_offset,
    run_report_with_retries,
    select_report_date,
)


class _FakeDataN:
    def __init__(self, offset_n: int):
        self._n = str(offset_n)

    async def get_attribute(self, attr: str):
        return self._n if attr == "data-n" else None


class _FakeDateItem:
    def __init__(self, n: int, text: str):
        self.n = n
        self._text = text
        self.clicked = False

    async def inner_text(self) -> str:
        return self._text

    async def query_selector(self, selector: str):
        if selector == "div[data-n]":
            return _FakeDataN(self.n)
        return None

    async def scroll_into_view_if_needed(self):
        pass

    async def click(self):
        self.clicked = True


class _FakeDateListFrame:
    def __init__(self, mapping: dict[str, str]):
        self._items = [_FakeDateItem(int(n), txt) for n, txt in mapping.items()]

    async def query_selector(self, selector: str):
        for item in self._items:
            if f"data-n='{item.n}'" in selector:
                return item
        return None

    async def query_selector_all(self, _selector: str):
        return list(self._items)


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


class TestResolveTargetStartOffset(unittest.IsolatedAsyncioTestCase):
    async def test_skips_future_dates_and_selects_exact_yesterday(self):
        # Future reservation dates present in SKIDATA (11/17, 11/16, 11/15)
        # Expected yesterday is 11/14 at data-n=3
        frame = _FakeDateListFrame({
            "0": "11/17/2026",
            "1": "11/16/2026",
            "2": "11/15/2026",
            "3": "11/14/2026",
            "4": "11/13/2026",
        })
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 14)
        )
        self.assertEqual(offset, 3)
        self.assertEqual(selected_date, date(2026, 11, 14))
        self.assertIsNotNone(item)

    async def test_uses_offset_0_when_today_is_missing_and_0_is_yesterday(self):
        frame = _FakeDateListFrame({"0": "11/12/2026", "1": "11/11/2026"})
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 12)
        )
        self.assertEqual(offset, 0)
        self.assertEqual(selected_date, date(2026, 11, 12))

    async def test_uses_offset_1_when_today_is_at_0_and_yesterday_is_at_1(self):
        frame = _FakeDateListFrame({"0": "11/13/2026", "1": "11/12/2026"})
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 12)
        )
        self.assertEqual(offset, 1)
        self.assertEqual(selected_date, date(2026, 11, 12))

    async def test_fallback_to_latest_past_date_when_yesterday_missing(self):
        # Expected yesterday 11/14 is missing due to delayed sync, latest is 11/13
        frame = _FakeDateListFrame({"0": "11/13/2026", "1": "11/12/2026"})
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 14)
        )
        self.assertEqual(offset, 0)
        self.assertEqual(selected_date, date(2026, 11, 13))

    async def test_matches_explicit_target_date(self):
        frame = _FakeDateListFrame({
            "0": "11/17/2026",
            "1": "11/16/2026",
            "2": "11/10/2026",
        })
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", target_date=date(2026, 11, 10)
        )
        self.assertEqual(offset, 2)
        self.assertEqual(selected_date, date(2026, 11, 10))

    async def test_raises_if_explicit_target_date_not_found(self):
        frame = _FakeDateListFrame({"0": "11/17/2026", "1": "11/16/2026"})
        with self.assertRaises(RuntimeError):
            await resolve_target_start_offset(
                frame, "div.RowColumn-barContainer", target_date=date(2026, 11, 1)
            )


class TestSelectReportDate(unittest.IsolatedAsyncioTestCase):
    @patch("scraper.find_frame_with_selector", new_callable=AsyncMock)
    async def test_selects_yesterday_ignoring_future_dates(self, mock_find_frame):
        mock_find_frame.return_value = None
        expected_yesterday = (datetime.now() - timedelta(days=1)).date()
        future_date = (datetime.now() + timedelta(days=2)).date().strftime("%m/%d/%Y")
        today_str = datetime.now().date().strftime("%m/%d/%Y")
        yesterday_str = expected_yesterday.strftime("%m/%d/%Y")

        frame = _FakeDateListFrame({
            "0": future_date,
            "1": today_str,
            "2": yesterday_str,
        })
        page = _FakePage()

        selected = await select_report_date(frame, "div.RowColumn-barContainer", page, confirm_required=False)
        self.assertEqual(selected, expected_yesterday)
        self.assertFalse(frame._items[0].clicked)
        self.assertFalse(frame._items[1].clicked)
        self.assertTrue(frame._items[2].clicked)


class TestRunReportWithRetries(unittest.IsolatedAsyncioTestCase):
    async def test_succeeds_on_first_try(self):
        opened_tabs = []
        closed_tabs = []
        flow_calls = []

        async def fake_open():
            tab_id = f"tab_{len(opened_tabs)+1}"
            opened_tabs.append(tab_id)
            return tab_id

        async def fake_flow(tab):
            flow_calls.append(tab)

        async def fake_cleanup(tab):
            closed_tabs.append(tab)

        await run_report_with_retries(
            "Test Report",
            fake_open,
            fake_flow,
            max_retries=2,
            cleanup_tab_fn=fake_cleanup,
            retry_delay_s=0.01,
        )

        self.assertEqual(opened_tabs, ["tab_1"])
        self.assertEqual(flow_calls, ["tab_1"])
        self.assertEqual(closed_tabs, ["tab_1"])

    async def test_succeeds_on_retry_after_transient_failure(self):
        opened_tabs = []
        closed_tabs = []
        attempts = 0

        async def fake_open():
            tab_id = f"tab_{len(opened_tabs)+1}"
            opened_tabs.append(tab_id)
            return tab_id

        async def fake_flow(tab):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"Transient Qlik lag on attempt {attempts}")

        async def fake_cleanup(tab):
            closed_tabs.append(tab)

        await run_report_with_retries(
            "Test Report",
            fake_open,
            fake_flow,
            max_retries=2,
            cleanup_tab_fn=fake_cleanup,
            retry_delay_s=0.01,
        )

        self.assertEqual(attempts, 3)
        self.assertEqual(opened_tabs, ["tab_1", "tab_2", "tab_3"])
        self.assertEqual(closed_tabs, ["tab_1", "tab_2", "tab_3"])

    async def test_fails_and_raises_after_max_retries_exhausted(self):
        opened_tabs = []
        closed_tabs = []
        attempts = 0

        async def fake_open():
            tab_id = f"tab_{len(opened_tabs)+1}"
            opened_tabs.append(tab_id)
            return tab_id

        async def fake_flow(tab):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Permanent error")

        async def fake_cleanup(tab):
            closed_tabs.append(tab)

        with self.assertRaises(RuntimeError) as ctx:
            await run_report_with_retries(
                "Test Report",
                fake_open,
                fake_flow,
                max_retries=2,
                cleanup_tab_fn=fake_cleanup,
                retry_delay_s=0.01,
            )

        self.assertIn("Failed after 3 attempts", str(ctx.exception))
        self.assertEqual(attempts, 3)
        self.assertEqual(opened_tabs, ["tab_1", "tab_2", "tab_3"])
        self.assertEqual(closed_tabs, ["tab_1", "tab_2", "tab_3"])


if __name__ == "__main__":
    unittest.main()
