"""Unit tests for safe Qlik virtual-listbox scrolling in scraper.py."""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scraper import (
    _find_listbox_item_by_offset,
    _wait_for_visible_element_across_all,
    find_date_item_by_date,
    resolve_target_start_offset,
    run_report_with_retries,
)


class _FakeWidget:
    def __init__(self, visible: bool):
        self._visible = visible

    async def is_visible(self) -> bool:
        return self._visible


class _FakeRoot:
    def __init__(self, mapping: dict[str, list[_FakeWidget]]):
        self._mapping = mapping

    async def query_selector_all(self, selector: str):
        return self._mapping.get(selector, [])


class _FakePage:
    def __init__(self, main_mapping: dict, frame_mappings: list[dict]):
        self._main = _FakeRoot(main_mapping)
        self.frames = [_FakeRoot(m) for m in frame_mappings]

    async def query_selector_all(self, selector: str):
        return await self._main.query_selector_all(selector)


class _FakeMouse:
    def __init__(self, on_wheel):
        self._on_wheel = on_wheel
        self.wheel_calls = 0

    async def move(self, _x: float, _y: float) -> None:
        return None

    async def wheel(self, _dx: float, dy: float) -> None:
        self.wheel_calls += 1
        if dy > 0:
            self._on_wheel()


class _FakeKeyboard:
    async def press(self, _key: str) -> None:
        return None


class _FakeDataN:
    def __init__(self, offset_n: int):
        self._n = str(offset_n)

    async def get_attribute(self, attr: str):
        return self._n if attr == "data-n" else None


class _FakeItem:
    def __init__(self, offset_n: int, on_scroll, allow_scroll_into_view: bool = True):
        self.offset_n = offset_n
        self._on_scroll = on_scroll
        self._allow_scroll_into_view = allow_scroll_into_view

    async def inner_text(self) -> str:
        return f"11/{self.offset_n:02d}/2026"

    async def query_selector(self, selector: str):
        if selector == "div[data-n]":
            return _FakeDataN(self.offset_n)
        return None

    async def bounding_box(self):
        return {"x": 100.0, "y": 200.0, "width": 120.0, "height": 28.0}

    async def scroll_into_view_if_needed(self) -> None:
        if self._allow_scroll_into_view:
            self._on_scroll()

    async def evaluate(self, _script: str) -> bool:
        return False


class _FakeListFrame:
    """Simulates a virtualized Qlik listbox where old DOM scroll selectors don't exist."""

    def __init__(
        self,
        max_offset: int = 30,
        window_size: int = 10,
        fail_eval_selector: bool = True,
        allow_scroll_into_view: bool = True,
    ):
        self.max_offset = max_offset
        self.window_size = window_size
        self.top_offset = 1
        self.fail_eval_selector = fail_eval_selector
        self.allow_scroll_into_view = allow_scroll_into_view
        self.scroll_calls = 0

    def _advance(self) -> None:
        self.scroll_calls += 1
        if self.top_offset + self.window_size <= self.max_offset:
            self.top_offset = min(self.top_offset + 3, self.max_offset - self.window_size + 1)

    def _visible_offsets(self):
        end = min(self.max_offset, self.top_offset + self.window_size - 1)
        return range(self.top_offset, end + 1)

    async def query_selector(self, selector: str):
        for n in self._visible_offsets():
            if f"data-n='{n}'" in selector:
                return _FakeItem(n, self._advance, self.allow_scroll_into_view)
        return None

    async def query_selector_all(self, _selector: str):
        return [_FakeItem(n, self._advance, self.allow_scroll_into_view) for n in self._visible_offsets()]

    async def eval_on_selector(self, selector: str, _script: str):
        if self.fail_eval_selector:
            raise RuntimeError(f"Frame.eval_on_selector: Failed to find element matching selector {selector!r}")


class TestVirtualListboxScroll(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_hit_without_scrolling(self):
        frame = _FakeListFrame(max_offset=20, window_size=10)
        item = await _find_listbox_item_by_offset(frame, "div.RowColumn-barContainer", 5)
        self.assertIsNotNone(item)
        self.assertEqual(item.offset_n, 5)
        self.assertEqual(frame.scroll_calls, 0)

    async def test_scrolls_to_day_15_via_mouse_wheel_even_when_dom_scroll_is_noop(self):
        frame = _FakeListFrame(
            max_offset=30,
            window_size=10,
            fail_eval_selector=True,
            allow_scroll_into_view=False,
        )
        page = _FakePage({}, [])
        page.mouse = _FakeMouse(frame._advance)
        page.keyboard = _FakeKeyboard()
        item = await _find_listbox_item_by_offset(
            frame, "div.RowColumn-barContainer", 15, max_scrolls=20, page=page
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.offset_n, 15)
        self.assertGreater(page.mouse.wheel_calls, 0)

    async def test_stops_early_when_list_stagnates(self):
        frame = _FakeListFrame(max_offset=10, window_size=10, fail_eval_selector=True)
        item = await _find_listbox_item_by_offset(frame, "div.RowColumn-barContainer", 99, max_scrolls=40)
        self.assertIsNone(item)
        self.assertLessEqual(frame.scroll_calls, 8)

    async def test_skips_hidden_elements_and_finds_visible_across_frames(self):
        hidden = _FakeWidget(visible=False)
        visible_in_frame = _FakeWidget(visible=True)
        page = _FakePage(
            main_mapping={"a.export-url": [hidden]},
            frame_mappings=[{}, {"a[href*='tempcontent']": [visible_in_frame]}],
        )
        res = await _wait_for_visible_element_across_all(
            page,
            ["a.export-url", "a[href*='tempcontent']"],
            timeout_s=0.3,
            check_interval=0.05,
        )
        self.assertIsNotNone(res)
        el, root = res
        self.assertIs(el, visible_in_frame)
        self.assertIs(root, page.frames[1])


class _FakeDateItem:
    def __init__(self, n: int, text: str):
        self.n = n
        self._text = text

    async def inner_text(self) -> str:
        return self._text

    async def query_selector(self, selector: str):
        if selector == "div[data-n]":
            return _FakeDataN(self.n)
        return None

    async def scroll_into_view_if_needed(self):
        pass

    async def click(self):
        pass


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


class TestResolveTargetStartOffset(unittest.IsolatedAsyncioTestCase):
    async def test_skips_future_dates_and_selects_exact_yesterday(self):
        from datetime import date
        # Future reservation dates present in SKIDATA (11/17, 11/16, 11/15)
        # Yesterday is 11/14 at data-n=3
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
        from datetime import date
        frame = _FakeDateListFrame({"0": "11/12/2026", "1": "11/11/2026"})
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 12)
        )
        self.assertEqual(offset, 0)
        self.assertEqual(selected_date, date(2026, 11, 12))

    async def test_uses_offset_1_when_today_is_at_0_and_yesterday_is_at_1(self):
        from datetime import date
        frame = _FakeDateListFrame({"0": "11/13/2026", "1": "11/12/2026"})
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 12)
        )
        self.assertEqual(offset, 1)
        self.assertEqual(selected_date, date(2026, 11, 12))

    async def test_fallback_to_latest_past_date_when_yesterday_missing(self):
        from datetime import date
        # Expected yesterday 11/14 is missing due to delayed sync, latest is 11/13
        frame = _FakeDateListFrame({"0": "11/13/2026", "1": "11/12/2026"})
        item, offset, selected_date = await resolve_target_start_offset(
            frame, "div.RowColumn-barContainer", expected_yesterday=date(2026, 11, 14)
        )
        self.assertEqual(offset, 0)
        self.assertEqual(selected_date, date(2026, 11, 13))

    async def test_matches_explicit_target_date(self):
        from datetime import date
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
        from datetime import date
        frame = _FakeDateListFrame({"0": "11/17/2026", "1": "11/16/2026"})
        with self.assertRaises(RuntimeError):
            await resolve_target_start_offset(
                frame, "div.RowColumn-barContainer", target_date=date(2026, 11, 1)
            )


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

        # 3 total attempts (attempt 1 fail, attempt 2 fail, attempt 3 succeed)
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
