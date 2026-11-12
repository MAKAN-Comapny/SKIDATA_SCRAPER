"""Unit tests for safe Qlik virtual-listbox scrolling in scraper.py."""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scraper import _find_listbox_item_by_offset, _wait_for_visible_element_across_all


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


class _FakeItem:
    def __init__(self, offset_n: int, on_scroll):
        self.offset_n = offset_n
        self._on_scroll = on_scroll

    async def inner_text(self) -> str:
        return f"11/{self.offset_n:02d}/2026"

    async def scroll_into_view_if_needed(self) -> None:
        self._on_scroll()

    async def evaluate(self, _script: str) -> None:
        return None


class _FakeListFrame:
    """Simulates a virtualized Qlik listbox where old DOM scroll selectors don't exist."""

    def __init__(self, max_offset: int = 30, window_size: int = 10, fail_eval_selector: bool = True):
        self.max_offset = max_offset
        self.window_size = window_size
        self.top_offset = 1
        self.fail_eval_selector = fail_eval_selector
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
                return _FakeItem(n, self._advance)
        return None

    async def query_selector_all(self, _selector: str):
        return [_FakeItem(n, self._advance) for n in self._visible_offsets()]

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

    async def test_scrolls_to_day_15_and_beyond_without_old_selector(self):
        frame = _FakeListFrame(max_offset=30, window_size=10, fail_eval_selector=True)
        item = await _find_listbox_item_by_offset(frame, "div.RowColumn-barContainer", 15, max_scrolls=20)
        self.assertIsNotNone(item)
        self.assertEqual(item.offset_n, 15)
        self.assertGreater(frame.scroll_calls, 0)

    async def test_stops_early_when_list_stagnates(self):
        frame = _FakeListFrame(max_offset=10, window_size=10, fail_eval_selector=True)
        item = await _find_listbox_item_by_offset(frame, "div.RowColumn-barContainer", 99, max_scrolls=40)
        self.assertIsNone(item)
        self.assertLessEqual(frame.scroll_calls, 4)

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


if __name__ == "__main__":
    unittest.main()
