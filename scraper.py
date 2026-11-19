import glob
import asyncio
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


LOGIN_URL_DEFAULT = (
    "https://car.webhost.skidata.com/analytics/"
    "?proxyRestUri=https%3a%2f%2faut-qlikc01n01.sdnsa.net%3a4243%2fqps%2fswebanalyze4analytics%2f"
    "&targetId=5778d56a-9b37-4797-b185-736ebc571f4c"
)

# Folder for exported files; use this path when uploading to another container.
EXPORTS_DIR = "exports"


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def find_frame_with_selector(
    page: Page,
    selector: str,
    per_frame_timeout_ms: int = 30_000,
    total_timeout_ms: Optional[int] = None,
) -> Optional[Frame]:
    """
    Search all frames for one that contains the given selector.

    Qlik Sense keeps adding iframes after the first paint, so we must refresh
    ``page.frames`` on each pass. A single ``for frame in page.frames`` snapshot
    can miss the frame that later receives the sheet list.

    ``per_frame_timeout_ms`` caps one wait_for_selector call on one frame.
    ``total_timeout_ms`` (default: max(180s, 2 * per_frame)) is the overall budget.
    """
    if total_timeout_ms is None:
        total_timeout_ms = max(180_000, per_frame_timeout_ms * 2)
    deadline = time.monotonic() + total_timeout_ms / 1000.0

    while time.monotonic() < deadline:
        frames = list(page.frames)
        for frame in frames:
            if time.monotonic() >= deadline:
                break
            remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
            timeout_ms = min(per_frame_timeout_ms, max(250, remaining_ms))
            try:
                await frame.wait_for_selector(selector, timeout=timeout_ms)
                return frame
            except PlaywrightTimeoutError:
                continue
        await asyncio.sleep(0.35)
    return None


AST_TIMEZONE = timezone(timedelta(hours=3))


def get_ast_now() -> datetime:
    """Return current datetime in Saudi Arabia Standard Time (AST = UTC+3)."""
    return datetime.now(AST_TIMEZONE)


def get_expected_yesterday() -> date:
    """Return yesterday's calendar date in Saudi Arabia Standard Time (AST)."""
    return (get_ast_now() - timedelta(days=1)).date()


def parse_target_date() -> Optional[date]:
    """TARGET_DATE=YYYY-MM-DD selects a specific report day. Empty means yesterday or rolling window."""
    raw = os.getenv("TARGET_DATE", "").strip()
    return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None


def parse_days_count() -> int:
    """DAYS_COUNT specifies how many rolling past days to fetch (default: 5)."""
    raw = os.getenv("DAYS_COUNT", "5").strip()
    try:
        val = int(raw)
        return max(1, val)
    except ValueError:
        return 5


def get_run_period_tag() -> str:
    """
    Determine run period based on Saudi Arabia Standard Time (AST = UTC+3):
    - Before 12:00 PM AST -> 'Morning'
    - 12:00 PM to 4:59 PM AST -> '12Noon'
    - 5:00 PM AST onwards -> 'Evening'
    Can be overridden via RUN_TAG env var (e.g. 'Morning', 'Evening', '12Noon', or 'none').
    """
    override = os.getenv("RUN_TAG", "").strip()
    if override:
        return "" if override.lower() in ("none", "false", "off", "0") else override
    ast_hour = get_ast_now().hour
    if ast_hour < 12:
        return "Morning"
    elif ast_hour < 17:
        return "12Noon"
    else:
        return "Evening"


def get_report_file_prefix(end_date: date, days_count: int = 1, include_period: bool = True) -> str:
    """
    Format date prefix for export files:
    - Uses the latest date and separates Morning/Evening runs:
      e.g. '17-Nov-26-Morning' or '17-Nov-26-Evening'
    """
    date_str = end_date.strftime("%d-%b-%y")
    if not include_period:
        return date_str
    tag = get_run_period_tag()
    return f"{date_str}-{tag}" if tag else date_str


def _parse_row_date(text: str) -> Optional[date]:
    """Date filter rows read like 10/22/2026 (month/day/year)."""
    try:
        return datetime.strptime(text.strip().split()[0], "%m/%d/%Y").date()
    except (ValueError, IndexError):
        return None


DOWNLOAD_LINK_SELECTORS = [
    "a.export-url",
    "a:has-text('download your data file')",
    "a:has-text('Click here')",
    "a[href*='tempcontent']",
    ".lui-dialog a",
]

MODAL_EXPORT_BUTTON_SELECTORS = [
    "button.lui-button:has-text('Export')",
    "button:has-text('Export')",
    ".lui-dialog button:has-text('Export')",
    "button[title='Export']",
    "span.lui-button__text:has-text('Export')",
]


async def _find_visible_element_across_all(page: Page, selectors: list[str]):
    """Return the first visible element matching any selector across main page and all iframes."""
    for root in [page] + list(page.frames):
        for sel in selectors:
            try:
                elements = await root.query_selector_all(sel)
                for el in elements:
                    if await el.is_visible():
                        return el, root
            except Exception:
                pass
    return None


async def _wait_for_visible_element_across_all(
    page: Page,
    selectors: list[str],
    timeout_s: float = 120.0,
    check_interval: float = 0.8,
):
    """Poll main page and all iframes until a visible matching element appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = await _find_visible_element_across_all(page, selectors)
        if found is not None:
            return found
        await asyncio.sleep(check_interval)
    return None


async def _close_any_dialog(page: Page) -> None:
    """Best-effort close of any open Qlik export/filter modal."""
    for sel_group in (["button:has-text('Close')", "button:has-text('Cancel')"],):
        found = await _find_visible_element_across_all(page, sel_group)
        if found is not None:
            btn, _ = found
            try:
                await btn.click(force=True)
                await asyncio.sleep(0.5)
            except Exception:
                pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _clear_qlik_selections(page: Page) -> None:
    """Best-effort clear of active Qlik selections when recovering between attempts."""
    for root in [page] + list(page.frames):
        try:
            for sel in [
                "button[data-testid='actions-toolbar-clear']",
                "button[title='Clear selection']",
                "button[title='Clear all selections']",
                "span.lui-icon--clear-selections",
            ]:
                for btn in await root.query_selector_all(sel):
                    if await btn.is_visible() and await btn.is_enabled():
                        await btn.click(force=True)
                        await asyncio.sleep(0.6)
                        return
        except Exception:
            pass


async def _trigger_export_and_download(
    page: Page,
    dest_path: str,
    timeout_s: float = 300.0,
) -> str:
    """
    Handles clicking modal Export (if shown), waiting for download link across page and all frames,
    triggering download, saving to dest_path, and closing dialogs.
    """
    btn_res = await _wait_for_visible_element_across_all(
        page, MODAL_EXPORT_BUTTON_SELECTORS, timeout_s=15.0, check_interval=0.4
    )
    if btn_res is not None:
        btn, _ = btn_res
        try:
            await btn.scroll_into_view_if_needed()
            await btn.click(force=True)
            print("Clicked modal Export button.")
        except Exception:
            pass
    else:
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    print("Waiting for export link to appear...")
    link_res = await _wait_for_visible_element_across_all(
        page, DOWNLOAD_LINK_SELECTORS, timeout_s=timeout_s, check_interval=1.0
    )
    if link_res is None:
        raise RuntimeError("Export link did not appear in any frame.")

    download_link, _ = link_res
    print("Export link found, starting download...")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

    async with page.expect_download(timeout=180_000) as download_info:
        await download_link.click(force=True)
    download = await download_info.value

    base, ext = os.path.splitext(dest_path)
    if not ext:
        sugg_ext = os.path.splitext(download.suggested_filename)[1] or ".xlsx"
        dest_path = f"{base}{sugg_ext}"

    if os.path.exists(dest_path):
        os.remove(dest_path)
    await download.save_as(dest_path)
    print(f"Download completed and saved to: {dest_path} ({os.path.getsize(dest_path):,} bytes)")

    await _close_any_dialog(page)
    await asyncio.sleep(0.5)
    return dest_path


async def _collect_visible_offsets(list_frame: Frame, listbox_base: str) -> tuple[list, list[str]]:
    """Return rendered listbox items and their data-n/text tokens for progress detection."""
    items = await list_frame.query_selector_all(listbox_base)
    tokens: list[str] = []
    for item in items:
        try:
            n_el = await item.query_selector("div[data-n]")
            n_val = await n_el.get_attribute("data-n") if n_el else None
            txt = (await item.inner_text()).strip().split()[0] if not n_val else f"n={n_val}"
            tokens.append(txt)
        except Exception:
            continue
    return items, tokens


async def _scroll_listbox_down(
    list_frame: Frame,
    items: list,
    page: Optional[Page] = None,
) -> None:
    """Scroll a virtualized Qlik listbox down using real scrollTop, mouse wheel, and fallback selectors."""
    scrolled_by_dom = False
    if items:
        # Pick an item near the bottom visually (in case virtual DOM recycles node order)
        target_item = items[-1]
        try:
            scrolled_by_dom = bool(
                await target_item.evaluate(
                    """el => {
                        let p = el.parentElement;
                        while (p) {
                            const before = p.scrollTop;
                            p.scrollTop = before + 320;
                            if (p.scrollTop > before) return true;
                            p = p.parentElement;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            pass

        if not scrolled_by_dom:
            try:
                await target_item.scroll_into_view_if_needed()
            except Exception:
                pass

        if page is not None:
            try:
                box = await items[len(items) // 2].bounding_box()
                if box:
                    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    await page.mouse.wheel(0, 450)
            except Exception:
                pass

    if not scrolled_by_dom:
        try:
            await list_frame.eval_on_selector(
                "div.RowColumn-scrollContainer, div.lui-virtual-scroll, div.lui-list, div[role='listbox']",
                "el => el.scrollBy(0, 320)",
            )
        except Exception:
            pass


async def _find_listbox_item_by_offset(
    list_frame: Frame,
    listbox_base: str,
    offset_n: int,
    max_scrolls: int = 40,
    page: Optional[Page] = None,
):
    """Locate a Qlik date listbox item by data-n offset, scrolling virtualized rows safely."""
    item_sel = f"{listbox_base}:has(div[data-n='{offset_n}'])"
    item = await list_frame.query_selector(item_sel)
    if item is not None:
        return item

    prev_sig: Optional[tuple[str, ...]] = None
    stagnant = 0
    last_tokens: list[str] = []
    for _ in range(max_scrolls):
        items, last_tokens = await _collect_visible_offsets(list_frame, listbox_base)
        sig = tuple(last_tokens)
        if sig and sig == prev_sig:
            stagnant += 1
            if page is not None and stagnant in (2, 4):
                try:
                    await page.keyboard.press("PageDown")
                except Exception:
                    pass
            if stagnant >= 6:
                break
        else:
            stagnant = 0
            prev_sig = sig

        await _scroll_listbox_down(list_frame, items, page=page)
        await asyncio.sleep(0.4)

        item = await list_frame.query_selector(item_sel)
        if item is not None:
            return item

    if last_tokens:
        print(f"Debug: Date listbox offsets visible when searching data-n={offset_n}: {last_tokens[:20]}")
    return None


async def find_date_item_by_date(
    list_frame: Frame,
    listbox_base: str,
    target_date: date,
    max_scrolls: int = 40,
    page: Optional[Page] = None,
) -> tuple[Optional[any], Optional[int], Optional[date]]:
    """
    Search listbox for target_date by inspecting row date text.
    Scrolls virtualized list if needed.
    Returns (row_item, offset_n, parsed_date).
    """
    prev_sig = None
    stagnant = 0

    for scroll_idx in range(max_scrolls):
        items, tokens = await _collect_visible_offsets(list_frame, listbox_base)
        for item in items:
            txt = (await item.inner_text()).strip()
            d = _parse_row_date(txt)
            if d == target_date:
                n_el = await item.query_selector("div[data-n]")
                n_val = int(await n_el.get_attribute("data-n")) if n_el else None
                return item, n_val, d

        sig = tuple(tokens)
        if sig and sig == prev_sig:
            stagnant += 1
            if stagnant >= 6:
                break
        else:
            stagnant = 0
            prev_sig = sig

        await _scroll_listbox_down(list_frame, items, page=page)
        await asyncio.sleep(0.3)

    return None, None, None


async def resolve_target_start_offset(
    list_frame: Frame,
    listbox_base: str,
    target_date: Optional[date] = None,
    expected_yesterday: Optional[date] = None,
    page: Optional[Page] = None,
) -> tuple[any, int, date]:
    """
    Find the item, offset_n, and date for target_date (or yesterday by default).
    Strictly ignores any future dates (> expected_yesterday) and today.
    If exact yesterday is not in SKIDATA, safely selects the latest date <= yesterday.
    Returns (item, offset_n, selected_date).
    """
    yesterday = expected_yesterday or get_expected_yesterday()
    wanted_date = target_date or yesterday

    # 1. Quick check visible items without scrolling
    items = await list_frame.query_selector_all(listbox_base)
    seen: dict[date, tuple[any, int]] = {}
    for item in items:
        txt = (await item.inner_text()).strip()
        d = _parse_row_date(txt)
        if d is not None:
            n_el = await item.query_selector("div[data-n]")
            n_val = int(await n_el.get_attribute("data-n")) if n_el else 0
            seen[d] = (item, n_val)
            if d == wanted_date:
                print(f"Target date {d} matched at data-n={n_val}.")
                return item, n_val, d

    # 2. If not found in first batch, scroll down to search
    item, n_val, d = await find_date_item_by_date(
        list_frame, listbox_base, wanted_date, max_scrolls=25, page=page
    )
    if item is not None and n_val is not None and d is not None:
        print(f"Target date {d} matched after scroll at data-n={n_val}.")
        return item, n_val, d

    # 3. If target_date was explicitly passed, it must match
    if target_date is not None:
        raise RuntimeError(f"Requested TARGET_DATE {target_date} not found in SKIDATA Date filter.")

    # 4. Fallback for yesterday: find latest available completed date <= yesterday
    # (Never pick future dates or today!)
    past_dates = [dt for dt in seen.keys() if dt <= yesterday]
    if past_dates:
        latest = max(past_dates)
        best_item, best_n = seen[latest]
        print(
            f"Notice: Expected yesterday ({yesterday}) not yet in SKIDATA. "
            f"Using latest available date {latest} at data-n={best_n}."
        )
        return best_item, best_n, latest

    raise RuntimeError(
        f"No valid dates <= {yesterday} found in SKIDATA listbox. "
        f"Seen dates: {list(seen.keys())}"
    )


async def select_report_date(
    list_frame: Frame, listbox_base: str, page: Page, confirm_required: bool
) -> date:
    """
    Select date(s) in the open Date filter list and return the reference date.
    If TARGET_DATE is set: selects that specific single date.
    Otherwise: selects a rolling window of DAYS_COUNT starting from yesterday (or latest <= yesterday).
    """
    target = parse_target_date()
    expected_yesterday = get_expected_yesterday()

    if target is not None:
        # Single specific target date mode
        item, n_val, row_date = await resolve_target_start_offset(
            list_frame, listbox_base, target_date=target, page=page
        )
        row_el = await item.query_selector('[role="row"]')
        already = row_el is not None and await row_el.get_attribute("aria-selected") == "true"
        if already:
            print(f"Date {row_date} is already selected. Closing filter without changes.")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            return row_date

        await item.scroll_into_view_if_needed()
        await item.click()

    else:
        # Rolling multi-day window mode (default: past 5 or 15 days)
        days_count = parse_days_count()
        item, start_offset, row_date = await resolve_target_start_offset(
            list_frame, listbox_base, expected_yesterday=expected_yesterday, page=page
        )
        end_offset = start_offset + days_count - 1
        print(f"Selecting {days_count} day(s) range (data-n={start_offset} to data-n={end_offset}) ending at {row_date}...")

        selected_count = 0
        for n in range(start_offset, end_offset + 1):
            row_item = await _find_listbox_item_by_offset(list_frame, listbox_base, n, page=page)

            if row_item is not None:
                row_el = await row_item.query_selector('[role="row"]')
                already = row_el is not None and await row_el.get_attribute("aria-selected") == "true"
                if not already:
                    await row_item.scroll_into_view_if_needed()
                    await row_item.click()
                    selected_count += 1
                    await asyncio.sleep(0.15)
                else:
                    selected_count += 1
            else:
                print(f"Warning: Date item data-n={n} not found in DOM.")

        print(f"Successfully selected {selected_count} day(s) in Date filter.")

    # Confirm selection
    confirm_selectors = [
        "button[data-testid='actions-toolbar-confirm'][title='Confirm selection']",
        "button[data-testid='actions-toolbar-confirm']",
        "button[title='Confirm selection']",
        "button[title*='Confirm']",
        "button.actions-toolbar-confirm",
        "button.lui-button:has(.lui-icon--tick)",
        "button.lui-button:has(.lui-icon--check)",
        ".lui-popover button[title*='Confirm']",
        "button:has(.lui-icon--tick)",
        "button:has(.lui-icon--check)",
        "button.lui-button.lui-button--success",
    ]
    confirmed = False
    confirm_element = await _find_visible_element_across_all(page, confirm_selectors)
    if confirm_element is not None:
        btn, _ = confirm_element
        try:
            print("Clicking Confirm selection button...")
            await btn.click()
            confirmed = True
        except Exception as exc:
            print(f"Warning: Failed clicking confirm button: {exc}")

    if not confirmed:
        for sel in confirm_selectors[:4]:
            confirm_frame = await find_frame_with_selector(
                page, sel, per_frame_timeout_ms=1_500, total_timeout_ms=3_000
            )
            if confirm_frame is not None:
                try:
                    await confirm_frame.click(sel)
                    confirmed = True
                    print(f"Confirmed selection via frame click ({sel}).")
                    break
                except Exception:
                    pass

    if not confirmed:
        print("Confirm button not found; pressing Enter to commit selection...")
        try:
            await page.keyboard.press("Enter")
            confirmed = True
        except Exception:
            pass

    if not confirmed:
        if confirm_required:
            raise RuntimeError("Confirm selection button not found.")
        else:
            await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)
    return row_date



async def run_in_depth_flow(
    page: Page,
    download_dir: str,
    sheet_display_name: str,
    file_base_name: str,
    header_selector: str,
) -> None:
    """
    In the reports tab: open the given sheet, apply date filter (second row),
    expand all, download as Data, and save with yesterday's date + file_base_name.
    header_selector: CSS selector for the pivot table header (e.g. "header#CwPV_title").
    """
    await page.evaluate("window.scrollTo(0, 0)")
    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except PlaywrightTimeoutError:
        await page.wait_for_load_state("load")
    await asyncio.sleep(3)

    # 1) Open the sheet card (prefer span[title] like Qlik hub tiles; then :has-text)
    sheet_selector_span = (
        f"div.qv-content-li-inner:has(span[title='{sheet_display_name}'])"
    )
    sheet_selector_text = f"div.qv-content-li-inner:has-text('{sheet_display_name}')"
    print(f"Waiting for '{sheet_display_name}' sheet card...")
    sheet_frame = await find_frame_with_selector(
        page, sheet_selector_span, per_frame_timeout_ms=8_000, total_timeout_ms=180_000
    )
    sheet_selector = sheet_selector_span
    if sheet_frame is None:
        sheet_frame = await find_frame_with_selector(
            page, sheet_selector_text, per_frame_timeout_ms=8_000, total_timeout_ms=180_000
        )
        sheet_selector = sheet_selector_text
    if sheet_frame is None:
        for frame in list(page.frames):
            try:
                titles = await frame.eval_on_selector_all(
                    "div.qv-content-li-inner",
                    "els => els.map(el => el.textContent && el.textContent.trim())",
                )
                if titles:
                    print(
                        f"Debug: frame {frame.url} qv-content-li-inner texts:",
                        titles[:25],
                    )
            except Exception:
                continue
        raise RuntimeError(
            f"Could not find '{sheet_display_name}' sheet card in any frame."
        )
    sheet_card = await sheet_frame.query_selector(sheet_selector)
    if sheet_card is None and sheet_selector == sheet_selector_span:
        sheet_card = await sheet_frame.query_selector(sheet_selector_text)
        if sheet_card is not None:
            sheet_selector = sheet_selector_text
    if sheet_card is None:
        raise RuntimeError("Sheet card element not found after frame detection.")
    await sheet_card.scroll_into_view_if_needed()
    await sheet_card.click()
    print(f"Opened '{sheet_display_name}' sheet.")

    # 2) Date filter: open, check if second row already selected; if not, select it and confirm
    date_button_selector = (
        "div.lui-button.qv-pt-meta-button:has(.meta-text[title='Date'])"
    )
    print("Waiting for Date filter button...")
    date_frame = await find_frame_with_selector(page, date_button_selector)
    if date_frame is None:
        raise RuntimeError("Could not find Date filter button in any frame.")
    await date_frame.click(date_button_selector)
    print("Opened Date selection list.")
    await asyncio.sleep(0.8)

    listbox_base = "div.RowColumn-barContainer[data-testid='listbox.item']"
    print("Waiting for date list (RowColumn-barContainer)...")
    list_frame = await find_frame_with_selector(page, listbox_base)
    if list_frame is None:
        raise RuntimeError("Date list (RowColumn-barContainer) not found in any frame.")
    selected_date = await select_report_date(list_frame, listbox_base, page, confirm_required=True)

    # 4) Open the pivot context menu by right-clicking the header.
    print(f"Right-clicking pivot table header ({header_selector}) to open context menu...")
    pivot_frame = await find_frame_with_selector(page, header_selector)
    if pivot_frame is None:
        raise RuntimeError("Could not find pivot table header (id='CwPV_title').")

    pivot_header = await pivot_frame.query_selector(header_selector)
    if pivot_header is None:
        raise RuntimeError("Pivot table header element not found after frame detection.")

    await pivot_header.scroll_into_view_if_needed()
    await pivot_header.click(button="right")

    # Context menu may appear in main page or a different frame.
    await asyncio.sleep(0.5)

    print("Choosing 'Expand / collapse' -> 'Expand all'...")
    expand_group_selector = "li#expand-collapse-group"
    expand_all_selector = "li#expand-all"

    menu_frame = await find_frame_with_selector(page, expand_group_selector)
    if menu_frame is None:
        raise RuntimeError("Context menu 'Expand / collapse' item not found in any frame.")
    await menu_frame.click(expand_group_selector)

    await asyncio.sleep(0.8)

    expand_all_el = await menu_frame.wait_for_selector(expand_all_selector, timeout=60_000)
    await expand_all_el.scroll_into_view_if_needed()
    await expand_all_el.click()
    print("Clicked 'Expand all'. Waiting for Qlik Sense pivot table calculation to complete...")

    # Give the table adequate time to expand all nested dimensions (especially on multi-day ranges)
    await asyncio.sleep(8)
    for _ in range(12):
        busy = await _find_visible_element_across_all(page, [".qv-busy", "[aria-busy='true']", ".lui-spinner"])
        if busy is None:
            break
        await asyncio.sleep(1.0)

    # 5) Open context menu again and choose Download as... -> Data
    print("Opening context menu again for export...")
    await pivot_header.click(button="right")

    await asyncio.sleep(0.5)

    export_group_selector = "li#export-group"
    export_selector = "li#export"

    export_frame_menu = await find_frame_with_selector(page, export_group_selector)
    if export_frame_menu is None:
        raise RuntimeError("Context menu 'Download as...' item not found in any frame.")
    await export_frame_menu.click(export_group_selector)

    await asyncio.sleep(0.5)

    export_item = await export_frame_menu.wait_for_selector(export_selector, timeout=60_000)
    await export_item.click()

    # 6) Wait for export to generate, then find and click the download link
    target_date = parse_target_date()
    days_count = parse_days_count() if target_date is None else 1
    date_prefix = get_report_file_prefix(selected_date, days_count)
    target_path = os.path.join(download_dir, f"{date_prefix}-{file_base_name}.xlsx")
    await _trigger_export_and_download(page, target_path, timeout_s=300.0)

    # Immediately verify that the export is a valid, non-empty, and expanded table
    file_size = os.path.getsize(target_path) if os.path.exists(target_path) else 0
    if file_size < 2_048:
        if os.path.exists(target_path):
            os.remove(target_path)
        raise RuntimeError(
            f"Export '{file_base_name}' was downloaded empty or truncated ({file_size} bytes < 2KB). "
            f"Retrying report flow!"
        )

    import openpyxl
    try:
        wb_check = openpyxl.load_workbook(target_path, read_only=True)
        ws_check = wb_check.active
        rows_iter = ws_check.iter_rows(values_only=True)
        r1 = [str(c).strip() for c in next(rows_iter, []) if c is not None]
        r2 = [str(c).strip() for c in next(rows_iter, []) if c is not None] if ws_check.max_row > 1 else []
        wb_check.close()
        headers = r1 + r2
        if "Date" not in headers:
            if os.path.exists(target_path):
                os.remove(target_path)
            raise RuntimeError(
                f"Export '{file_base_name}' missing required 'Date' column (found: {r1}). "
                f"Pivot table was not fully expanded. Retrying report flow!"
            )
    except Exception as check_exc:
        if "missing required 'Date' column" in str(check_exc) or "collapsed state" in str(check_exc):
            raise
        print(f"Notice: Header check on {file_base_name}: {check_exc}")

    print(f"Exports folder (for upload): {os.path.abspath(download_dir)}")


async def _export_parking_single_day(
    page: Page,
    list_frame: Frame,
    listbox_base: str,
    n: int,
    prev_n: Optional[int],
    download_dir: str,
    header_selector: str,
) -> str:
    """Exports one day of Parking Transactions to avoid Qlik's 100,000 row calculation limit."""
    # Deselect any active days != n (Qlik floats selected rows to the top, plus prev_n fallback)
    selected_items = await list_frame.query_selector_all(
        f"{listbox_base}:has([role='row'][aria-selected='true'])"
    )
    deselected_prev = False
    for sel_item in selected_items:
        n_el = await sel_item.query_selector("div[data-n]")
        sel_n = await n_el.get_attribute("data-n") if n_el else None
        if sel_n != str(n):
            await sel_item.scroll_into_view_if_needed()
            await sel_item.click()
            await asyncio.sleep(0.2)
            if prev_n is not None and sel_n == str(prev_n):
                deselected_prev = True

    if prev_n is not None and not deselected_prev:
        prev_item = await _find_listbox_item_by_offset(list_frame, listbox_base, prev_n, page=page)
        if prev_item:
            row_el = await prev_item.query_selector('[role="row"]')
            if row_el and await row_el.get_attribute("aria-selected") == "true":
                await prev_item.scroll_into_view_if_needed()
                await prev_item.click()
                await asyncio.sleep(0.2)

    # Select current day n
    item = await _find_listbox_item_by_offset(list_frame, listbox_base, n, page=page)

    if not item:
        raise RuntimeError(f"Parking Transactions date item data-n={n} not found.")

    row_el = await item.query_selector('[role="row"]')
    if not (row_el and await row_el.get_attribute("aria-selected") == "true"):
        await item.scroll_into_view_if_needed()
        await item.click()
        await asyncio.sleep(0.2)

    # Confirm selection
    confirm_button_selector = "button[data-testid='actions-toolbar-confirm'][title='Confirm selection']"
    confirm_frame = await find_frame_with_selector(
        page, confirm_button_selector, per_frame_timeout_ms=2_000, total_timeout_ms=6_000
    )
    if confirm_frame:
        await confirm_frame.click(confirm_button_selector)
    else:
        await page.keyboard.press("Escape")
    await asyncio.sleep(2)

    # Right click header
    pivot_frame = await find_frame_with_selector(
        page, header_selector, per_frame_timeout_ms=6_000, total_timeout_ms=45_000
    )
    if not pivot_frame:
        pivot_frame = await find_frame_with_selector(
            page,
            "article:not(.qv-object-filterpane) header.qv-object-header",
            per_frame_timeout_ms=6_000,
            total_timeout_ms=45_000,
        )
    if not pivot_frame:
        raise RuntimeError(f"Could not find Parking Transactions header for offset {n}")

    header_el = await pivot_frame.query_selector(header_selector)
    if not header_el:
        header_el = await pivot_frame.query_selector("article:not(.qv-object-filterpane) header.qv-object-header")
    if not header_el:
        raise RuntimeError(f"Parking Transactions header element missing for offset {n}")

    await header_el.scroll_into_view_if_needed()
    await header_el.click(button="right")
    await asyncio.sleep(1.0)

    # Download as -> Data
    export_group_selector = "li#export-group"
    menu_frame = await find_frame_with_selector(
        page, export_group_selector, per_frame_timeout_ms=6_000, total_timeout_ms=45_000
    )
    if not menu_frame:
        raise RuntimeError(f"Context menu 'Download as...' not found for offset {n}")
    await menu_frame.click(export_group_selector)
    await asyncio.sleep(0.8)

    export_selector = "li#export"
    export_item = await menu_frame.wait_for_selector(export_selector, timeout=45_000)
    await export_item.click()
    await asyncio.sleep(1)

    temp_path = os.path.join(download_dir, f"temp_parking_day_{n}.xlsx")
    await _trigger_export_and_download(page, temp_path, timeout_s=180.0)
    print(f"    Saved day offset {n}: {os.path.getsize(temp_path):,} bytes")
    return temp_path


async def _open_date_listbox(page: Page, listbox_base: str, total_timeout_ms: int = 20_000) -> Frame:
    """Open the Qlik Date filter popover and return the frame containing listbox items."""
    # If listbox is already open and visible, return its frame directly
    existing_frame = await find_frame_with_selector(
        page, listbox_base, per_frame_timeout_ms=500, total_timeout_ms=1_000
    )
    if existing_frame is not None:
        return existing_frame

    date_filter_selector = "div[data-testid='collapsed-title-Date']"
    date_frame = await find_frame_with_selector(
        page, date_filter_selector, per_frame_timeout_ms=2_000, total_timeout_ms=total_timeout_ms
    )
    if date_frame is None:
        date_filter_selector = "h6[aria-label='Date']"
        date_frame = await find_frame_with_selector(
            page, date_filter_selector, per_frame_timeout_ms=2_000, total_timeout_ms=total_timeout_ms
        )
    if date_frame is None:
        raise RuntimeError("Could not find Date filter in any frame.")
    await date_frame.click(date_filter_selector)
    await asyncio.sleep(0.8)

    list_frame = await find_frame_with_selector(
        page, listbox_base, per_frame_timeout_ms=2_000, total_timeout_ms=total_timeout_ms
    )
    if list_frame is None:
        raise RuntimeError("Date listbox items did not render in any frame.")
    return list_frame


async def run_parking_transactions_flow(
    page: Page,
    download_dir: str,
) -> None:
    """
    In the reports tab: open Parking Transactions sheet.
    Because Qlik enforces a hard calculation limit (< 100,000 transactions) on this table,
    multi-day extractions iterate day-by-day and merge the rows into a consolidated workbook.
    """
    from excel_merge import merge_parking_xlsx
    sheet_display_name = "Parking Transactions"
    file_base_name = "Parking-Transactions"
    header_selector = "header#VvpsUS_title"

    await page.evaluate("window.scrollTo(0, 0)")
    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except PlaywrightTimeoutError:
        await page.wait_for_load_state("load")
    await asyncio.sleep(3)

    # 1) Open the Parking Transactions sheet card
    sheet_selector = "div.qv-content-li-inner:has(span[title='Parking Transactions'])"
    print(f"Waiting for '{sheet_display_name}' sheet card...")
    sheet_frame = await find_frame_with_selector(page, sheet_selector)
    if sheet_frame is None:
        raise RuntimeError(
            f"Could not find '{sheet_display_name}' sheet card in any frame."
        )
    sheet_card = await sheet_frame.query_selector(sheet_selector)
    if sheet_card is None:
        raise RuntimeError("Sheet card element not found after frame detection.")
    await sheet_card.scroll_into_view_if_needed()
    await sheet_card.click()
    print(f"Opened '{sheet_display_name}' sheet.")
    await asyncio.sleep(4)

    target_date = parse_target_date()
    days_count = parse_days_count() if target_date is None else 1
    yesterday = get_expected_yesterday()
    date_prefix = get_report_file_prefix(target_date or yesterday, days_count)
    target_path = os.path.join(download_dir, f"{date_prefix}-{file_base_name}.xlsx")
    os.makedirs(download_dir, exist_ok=True)

    listbox_base = "div.RowColumn-barContainer[data-testid='listbox.item']"

    if days_count == 1:
        # Standard single day extraction
        list_frame = await _open_date_listbox(page, listbox_base, total_timeout_ms=60_000)
        if target_date is not None:
            _, start_offset, detected_date = await resolve_target_start_offset(
                list_frame, listbox_base, target_date=target_date, page=page
            )
        else:
            _, start_offset, detected_date = await resolve_target_start_offset(
                list_frame, listbox_base, expected_yesterday=yesterday, page=page
            )
        if detected_date is not None:
            date_prefix = get_report_file_prefix(detected_date, 1)
            target_path = os.path.join(download_dir, f"{date_prefix}-{file_base_name}.xlsx")
        single_temp = await _export_parking_single_day(
            page, list_frame, listbox_base, n=start_offset, prev_n=None, download_dir=download_dir, header_selector=header_selector
        )
        if os.path.exists(target_path):
            os.remove(target_path)
        os.rename(single_temp, target_path)
        print(f"Parking Transactions saved to: {target_path}")

    else:
        # Multi-day rolling extraction with day-by-day isolation to respect < 100k threshold
        print(f"Exporting Parking Transactions for {days_count} days sequentially to avoid 100k row limit...")
        day_files = []
        prev_n = None

        # Determine start_offset dynamically based on resolved target date (bypassing any future dates)
        list_frame = await _open_date_listbox(page, listbox_base, total_timeout_ms=60_000)
        _, start_offset, detected_date = await resolve_target_start_offset(
            list_frame, listbox_base, expected_yesterday=yesterday, page=page
        )
        if detected_date is not None:
            date_prefix = get_report_file_prefix(detected_date, days_count)
            target_path = os.path.join(download_dir, f"{date_prefix}-{file_base_name}.xlsx")

        for idx, n in enumerate(range(start_offset, start_offset + days_count), start=1):
            print(f"  Fetching day {idx}/{days_count} (offset {n})...")
            last_exc: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    list_frame = await _open_date_listbox(page, listbox_base)
                    fp = await _export_parking_single_day(
                        page, list_frame, listbox_base, n=n, prev_n=prev_n, download_dir=download_dir, header_selector=header_selector
                    )
                    day_files.append(fp)
                    prev_n = n
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    print(f"  [Attempt {attempt}/3] Offset {n} failed ({exc!r}); resetting Qlik dialogs/selections...")
                    await _close_any_dialog(page)
                    await _clear_qlik_selections(page)
                    prev_n = None
                    await asyncio.sleep(2)
            if last_exc is not None:
                raise last_exc

        print("Merging day files into consolidated Parking-Transactions...")
        total_rows = merge_parking_xlsx(day_files, target_path)
        print(f"SUCCESS: Saved {total_rows} total rows across {days_count} days to: {target_path}")

        for f in day_files:
            if os.path.exists(f):
                os.remove(f)


async def run_report_with_retries(
    report_name: str,
    open_tab_fn,
    flow_coro_fn,
    max_retries: int = 2,
    cleanup_tab_fn=None,
    retry_delay_s: float = 3.0,
) -> None:
    """
    Executes a report flow with up to max_retries retry attempts (max_retries + 1 total).
    Opens a clean reports tab per attempt and cleans up afterwards.
    """
    total_attempts = max_retries + 1
    last_err = None
    for attempt in range(1, total_attempts + 1):
        rep_page = None
        try:
            print(f"[{report_name}] Starting execution (attempt {attempt}/{total_attempts})...")
            rep_page = await open_tab_fn()
            await flow_coro_fn(rep_page)
            print(f"[{report_name}] Successfully completed on attempt {attempt}.")
            return
        except Exception as exc:
            last_err = exc
            print(f"[{report_name}] Attempt {attempt}/{total_attempts} failed: {exc!r}")
            if attempt < total_attempts:
                print(f"[{report_name}] Retrying after cleanup...")
                await asyncio.sleep(retry_delay_s)
        finally:
            if rep_page is not None:
                if cleanup_tab_fn is not None:
                    try:
                        await cleanup_tab_fn(rep_page)
                    except Exception:
                        pass
                else:
                    try:
                        if hasattr(rep_page, "is_closed") and not rep_page.is_closed():
                            await rep_page.close()
                    except Exception:
                        pass

    raise RuntimeError(f"[{report_name}] Failed after {total_attempts} attempts. Last error: {last_err}") from last_err


async def login_and_open_portal() -> None:
    """
    - Open the login page
    - Fill in credentials
    - Submit form
    - Wait for portal card 'Parking_Dashboards_SA_Makan_Analyze'
    - Click the card and wait for new tab (reports)
    """
    load_dotenv()

    login_url = get_env("SKIDATA_URL", LOGIN_URL_DEFAULT)
    tenant = get_env("SKIDATA_TENANT")
    login_name = get_env("SKIDATA_LOGIN")
    password = get_env("SKIDATA_PASSWORD")

    headless = os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")

    async with async_playwright() as p:
        # Headless=True for CI (e.g. GitHub Actions); headless=False for local dev.
        # Give it a large viewport so the full sheet (including the pivot)
        # fits on screen and we avoid extra scrolling when hovering elements.
        browser = await p.chromium.launch(headless=headless)
        context: BrowserContext = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
        )

        project_root = os.path.dirname(os.path.abspath(__file__))
        download_dir = os.path.join(project_root, EXPORTS_DIR)

        page: Page = await context.new_page()
        print(f"Opening login page: {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded")

        # Fill in the login form fields based on provided HTML.
        # <input id="tenantName" name="tenantName">
        # <input id="loginName" name="loginName">
        # <input id="password" name="password">
        await page.fill("#tenantName", tenant)
        await page.fill("#loginName", login_name)
        await page.fill("#password", password)

        print("Submitting login form...")
        # Click Sign In button inside the form
        await page.click("form.login_form button[type='submit']")

        # Wait for the hub / portal page to load.
        # We wait specifically for the card container that has the app name text.
        # Based on the HTML you provided, the clickable container is `div.qv-content-li-inner`
        # and it contains the text "Parking_Dashboards_SA_Makan_Analyze" inside a
        # `h3.qv-text > span.q-ellipsis`.
        card_container_selector = (
            "div.qv-content-li-inner:has-text('Parking_Dashboards_SA_Makan_Analyze')"
        )

        print("Waiting for portal card 'Parking_Dashboards_SA_Makan_Analyze'...")

        # The card may be inside an iframe (Qlik hub often loads in a frame),
        # so search all frames, not just the main page.
        await page.wait_for_load_state("networkidle")

        target_frame = None
        for frame in page.frames:
            try:
                print(f"Checking frame for portal card: {frame.url}")
                await frame.wait_for_selector(card_container_selector, timeout=30_000)
                target_frame = frame
                break
            except PlaywrightTimeoutError:
                continue

        if target_frame is None:
            print("Could not find portal card in any frame. Debugging information:")
            for frame in page.frames:
                try:
                    titles = await frame.eval_on_selector_all(
                        "div.qv-content-li-inner",
                        "els => els.map(el => el.textContent && el.textContent.trim())",
                    )
                    if titles:
                        print(
                            f"Frame {frame.url} has qv-content-li-inner text blocks:",
                            titles,
                        )
                except Exception:
                    # Ignore eval errors in frames that don't allow it.
                    continue

            print(
                "Leaving browser open for manual inspection. "
                "Check what appears after login and share details so we can refine selectors."
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            except KeyboardInterrupt:
                print("Closing browser...")
            finally:
                await browser.close()
            return

        # The card container itself is clickable (`div.qv-content-li-inner`).
        card_element = await target_frame.query_selector(card_container_selector)
        if card_element is None:
            raise RuntimeError("Portal card element not found after it appeared.")

        # Scroll card into view just in case.
        await card_element.scroll_into_view_if_needed()

        # Helper to open a fresh reports tab from the hub card
        async def _open_fresh_reports_tab() -> Page:
            print("Opening new reports tab from portal card...")
            await page.bring_to_front()
            await _close_any_dialog(page)
            await card_element.scroll_into_view_if_needed()
            await card_element.click()
            new_page_promise = context.wait_for_event("page", timeout=60_000)
            try:
                rep_page = await new_page_promise
            except Exception as exc:
                raise RuntimeError("No new tab opened after clicking portal card.") from exc

            await rep_page.wait_for_load_state("domcontentloaded")
            print("New reports tab detected. URL:", rep_page.url)
            try:
                await rep_page.wait_for_load_state("networkidle", timeout=60_000)
            except PlaywrightTimeoutError:
                await rep_page.wait_for_load_state("load")
            await asyncio.sleep(4)
            return rep_page

        async def _cleanup_reports_tab(rep_page: Page) -> None:
            if rep_page is not None and not rep_page.is_closed():
                try:
                    await rep_page.close()
                except Exception:
                    pass
            await page.bring_to_front()
            await asyncio.sleep(1)

        import glob

        # 1) Revenue in Depth Analysis
        if not glob.glob(os.path.join(download_dir, "*Revenue-In-Depth.xlsx")):
            await run_report_with_retries(
                "Revenue in Depth Analysis",
                _open_fresh_reports_tab,
                lambda p: run_in_depth_flow(
                    p,
                    download_dir,
                    "Revenue in Depth Analysis",
                    "Revenue-In-Depth",
                    "header#CwPV_title",
                ),
                max_retries=2,
                cleanup_tab_fn=_cleanup_reports_tab,
            )
        else:
            print("Revenue-In-Depth already exported. Skipping.")

        # 2) Access in Depth Analysis
        if not glob.glob(os.path.join(download_dir, "*Access-In-Depth.xlsx")):
            await run_report_with_retries(
                "Access in Depth Analysis",
                _open_fresh_reports_tab,
                lambda p: run_in_depth_flow(
                    p,
                    download_dir,
                    "Access in Depth Analysis",
                    "Access-In-Depth",
                    "header#b8cfe7c0-f39f-45ce-8529-5edd3499c57b_title",
                ),
                max_retries=2,
                cleanup_tab_fn=_cleanup_reports_tab,
            )
        else:
            print("Access-In-Depth already exported. Skipping.")

        # 3) System Event in Depth Analysis
        if not glob.glob(os.path.join(download_dir, "*System-Event-In-Depth.xlsx")):
            await run_report_with_retries(
                "System Event in Depth Analysis",
                _open_fresh_reports_tab,
                lambda p: run_in_depth_flow(
                    p,
                    download_dir,
                    "System Event in Depth Analysis",
                    "System-Event-In-Depth",
                    "header#jKmZn_title",
                ),
                max_retries=2,
                cleanup_tab_fn=_cleanup_reports_tab,
            )
        else:
            print("System-Event-In-Depth already exported. Skipping.")

        # 4) Parking Transactions
        if not glob.glob(os.path.join(download_dir, "*Parking-Transactions.xlsx")):
            await run_report_with_retries(
                "Parking Transactions",
                _open_fresh_reports_tab,
                lambda p: run_parking_transactions_flow(p, download_dir),
                max_retries=2,
                cleanup_tab_fn=_cleanup_reports_tab,
            )
        else:
            print("Parking-Transactions already exported. Skipping.")

        await asyncio.sleep(2)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(login_and_open_portal())

