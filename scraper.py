import glob
import asyncio
import os
import time
from datetime import date, datetime, timedelta
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


def _parse_row_date(text: str) -> Optional[date]:
    """Date filter rows read like 10/22/2026 (month/day/year)."""
    try:
        return datetime.strptime(text.strip().split()[0], "%m/%d/%Y").date()
    except (ValueError, IndexError):
        return None


async def select_report_date(
    list_frame: Frame, listbox_base: str, page: Page, confirm_required: bool
) -> date:
    """
    Select date(s) in the open Date filter list and return the reference date (yesterday).
    If TARGET_DATE is set: selects that specific single date.
    Otherwise: selects a rolling window of DAYS_COUNT (default: 5 days, from data-n=1 to data-n=DAYS_COUNT).
    """
    target = parse_target_date()
    expected_yesterday = (datetime.now() - timedelta(days=1)).date()
    row_date: Optional[date] = expected_yesterday

    if target is not None:
        # Single specific target date mode
        row_item = None
        seen: set = set()
        for _ in range(40):
            items = await list_frame.query_selector_all(listbox_base)
            before = len(seen)
            for item in items:
                d = _parse_row_date(await item.inner_text())
                if d is not None:
                    seen.add(d)
                if d == target:
                    row_item, row_date = item, d
                    break
            if row_item is not None:
                break
            if not items or (len(seen) == before and seen):
                break  # the list stopped growing
            await items[-1].scroll_into_view_if_needed()
            await asyncio.sleep(0.6)
        if row_item is None:
            if seen:
                raise RuntimeError(
                    f"Date {target} is not in the portal list (seen {min(seen)} to {max(seen)})."
                )
            raise RuntimeError(f"Date {target} is not in the portal list.")
        print(f"Selecting {target} from the Date list.")

        row_el = await row_item.query_selector('[role="row"]')
        already = row_el is not None and await row_el.get_attribute("aria-selected") == "true"
        if already:
            print(f"Date {row_date} is already selected. Closing filter without changes.")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            return row_date

        await row_item.click()

    else:
        # Rolling multi-day window mode (default: past 5 days)
        days_count = parse_days_count()
        start_offset = 1  # 1 = yesterday (0 = today)
        end_offset = start_offset + days_count - 1
        print(f"Selecting {days_count} day(s) range (data-n={start_offset} to data-n={end_offset})...")

        selected_count = 0
        for n in range(start_offset, end_offset + 1):
            item_sel = f"{listbox_base}:has(div[data-n='{n}'])"
            row_item = await list_frame.query_selector(item_sel)

            # Virtual scroll fallback if item is not rendered yet in DOM
            if row_item is None:
                for _ in range(5):
                    await list_frame.eval_on_selector(
                        "div.RowColumn-scrollContainer, div.lui-virtual-scroll, div.lui-list",
                        "el => el.scrollBy(0, 150)",
                    )
                    await asyncio.sleep(0.2)
                    row_item = await list_frame.query_selector(item_sel)
                    if row_item:
                        break

            if row_item is not None:
                # Capture the exact date of yesterday (data-n=1) for naming
                if n == start_offset:
                    parsed_d = _parse_row_date(await row_item.inner_text())
                    if parsed_d is not None:
                        row_date = parsed_d

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
    confirm_button_selector = (
        "button[data-testid='actions-toolbar-confirm'][title='Confirm selection']"
    )
    confirm_frame = await find_frame_with_selector(
        page, confirm_button_selector, per_frame_timeout_ms=5_000
    )
    if confirm_frame is not None:
        print("Clicking Confirm selection button...")
        await confirm_frame.click(confirm_button_selector)
    elif confirm_required:
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
    await expand_all_el.click()

    # Give the table some time to expand
    await asyncio.sleep(2)

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
    print("Waiting for export to complete and download link to appear...")
    await asyncio.sleep(15)

    export_link_selector = "a.export-url"
    export_frame = await find_frame_with_selector(page, export_link_selector, total_timeout_ms=600_000, per_frame_timeout_ms=2_000)
    if export_frame is None:
        raise RuntimeError("Export link did not appear in any frame.")

    await export_frame.wait_for_selector(export_link_selector, timeout=300_000)

    print("Export link found, starting download...")
    os.makedirs(download_dir, exist_ok=True)

    async with page.expect_download() as download_info:
        await export_frame.click(export_link_selector)
    download = await download_info.value
    ext = os.path.splitext(download.suggested_filename)[1] or ".xlsx"
    date_prefix = selected_date.strftime("%d-%b-%y")  # e.g. 15-Feb-26
    target_path = os.path.join(download_dir, f"{date_prefix}-{file_base_name}{ext}")
    await download.save_as(target_path)
    print(f"Download completed and saved to: {target_path}")
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
    # Deselect previous day if applicable
    if prev_n is not None:
        prev_sel = f"{listbox_base}:has(div[data-n='{prev_n}'])"
        prev_item = await list_frame.query_selector(prev_sel)
        if prev_item:
            row_el = await prev_item.query_selector('[role="row"]')
            if row_el and await row_el.get_attribute("aria-selected") == "true":
                await prev_item.scroll_into_view_if_needed()
                await prev_item.click()
                await asyncio.sleep(0.2)

    # Select current day n
    item_sel = f"{listbox_base}:has(div[data-n='{n}'])"
    item = await list_frame.query_selector(item_sel)
    if not item:
        for _ in range(5):
            await list_frame.eval_on_selector(
                "div.RowColumn-scrollContainer, div.lui-virtual-scroll, div.lui-list",
                "el => el.scrollBy(0, 150)",
            )
            await asyncio.sleep(0.2)
            item = await list_frame.query_selector(item_sel)
            if item:
                break

    if not item:
        raise RuntimeError(f"Parking Transactions date item data-n={n} not found.")

    row_el = await item.query_selector('[role="row"]')
    if not (row_el and await row_el.get_attribute("aria-selected") == "true"):
        await item.scroll_into_view_if_needed()
        await item.click()
        await asyncio.sleep(0.2)

    # Confirm selection
    confirm_button_selector = "button[data-testid='actions-toolbar-confirm'][title='Confirm selection']"
    confirm_frame = await find_frame_with_selector(page, confirm_button_selector, total_timeout_ms=5_000)
    if confirm_frame:
        await confirm_frame.click(confirm_button_selector)
    await asyncio.sleep(2)

    # Right click header
    pivot_frame = await find_frame_with_selector(page, header_selector, total_timeout_ms=15_000)
    if not pivot_frame:
        pivot_frame = await find_frame_with_selector(
            page, "article:not(.qv-object-filterpane) header.qv-object-header", total_timeout_ms=15_000
        )
    header_el = await pivot_frame.query_selector(header_selector)
    if not header_el:
        header_el = await pivot_frame.query_selector("article:not(.qv-object-filterpane) header.qv-object-header")
    await header_el.scroll_into_view_if_needed()
    await header_el.click(button="right")
    await asyncio.sleep(0.5)

    # Download as -> Data
    export_group_selector = "li#export-group"
    menu_frame = await find_frame_with_selector(page, export_group_selector, total_timeout_ms=10_000)
    await menu_frame.click(export_group_selector)
    await asyncio.sleep(0.5)

    export_selector = "li#export"
    export_item = await menu_frame.wait_for_selector(export_selector, timeout=10_000)
    await export_item.click()
    await asyncio.sleep(1)

    # Click export button
    export_button_selector = "button.lui-button:has-text('Export')"
    export_button_frame = await find_frame_with_selector(page, export_button_selector, total_timeout_ms=5_000)
    btn = await export_button_frame.query_selector(export_button_selector)
    await btn.click()

    # Wait for download link
    export_link_selector = "a.export-url"
    export_frame = await find_frame_with_selector(page, export_link_selector, total_timeout_ms=60_000)
    if not export_frame:
        raise RuntimeError(f"Export link not found for Parking Transactions offset {n}")

    async with page.expect_download() as download_info:
        await export_frame.click(export_link_selector)
    download = await download_info.value

    # Close modal
    close_btn = await export_frame.query_selector("button:has-text('Close')")
    if close_btn:
        await close_btn.click()

    temp_path = os.path.join(download_dir, f"temp_parking_day_{n}.xlsx")
    await download.save_as(temp_path)
    return temp_path


async def run_parking_transactions_flow(
    page: Page,
    download_dir: str,
) -> None:
    """
    In the reports tab: open Parking Transactions sheet.
    Because Qlik enforces a hard calculation limit (< 100,000 transactions) on this table,
    multi-day extractions iterate day-by-day and merge the rows into a consolidated workbook.
    """
    import pandas as pd
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
    yesterday = (datetime.now() - timedelta(days=1)).date()
    date_prefix = (target_date or yesterday).strftime("%d-%b-%y")
    target_path = os.path.join(download_dir, f"{date_prefix}-{file_base_name}.xlsx")
    os.makedirs(download_dir, exist_ok=True)

    listbox_base = "div.RowColumn-barContainer[data-testid='listbox.item']"

    if days_count == 1:
        # Standard single day extraction
        date_filter_selector = "div[data-testid='collapsed-title-Date']"
        date_frame = await find_frame_with_selector(page, date_filter_selector)
        if date_frame is None:
            date_filter_selector = "h6[aria-label='Date']"
            date_frame = await find_frame_with_selector(page, date_filter_selector)
        if date_frame is None:
            raise RuntimeError("Could not find Date filter in any frame.")
        await date_frame.click(date_filter_selector)
        await asyncio.sleep(0.8)

        list_frame = await find_frame_with_selector(page, listbox_base)
        single_temp = await _export_parking_single_day(
            page, list_frame, listbox_base, n=1, prev_n=None, download_dir=download_dir, header_selector=header_selector
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

        for n in range(1, days_count + 1):
            print(f"  Fetching day {n}/{days_count} (offset {n})...")
            date_filter_selector = "div[data-testid='collapsed-title-Date']"
            date_frame = await find_frame_with_selector(page, date_filter_selector, total_timeout_ms=15_000)
            if not date_frame:
                date_filter_selector = "h6[aria-label='Date']"
                date_frame = await find_frame_with_selector(page, date_filter_selector, total_timeout_ms=15_000)
            await date_frame.click(date_filter_selector)
            await asyncio.sleep(0.8)

            list_frame = await find_frame_with_selector(page, listbox_base, total_timeout_ms=15_000)
            fp = await _export_parking_single_day(
                page, list_frame, listbox_base, n=n, prev_n=prev_n, download_dir=download_dir, header_selector=header_selector
            )
            day_files.append(fp)
            prev_n = n

        print("Merging day files into consolidated Parking-Transactions...")
        dfs = [pd.read_excel(f) for f in day_files]
        merged_df = pd.concat(dfs, ignore_index=True)
        merged_df.to_excel(target_path, index=False)
        print(f"SUCCESS: Saved {len(merged_df)} total rows across {days_count} days to: {target_path}")

        for f in day_files:
            if os.path.exists(f):
                os.remove(f)


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

        # Scroll into view and click anywhere in the card container to open the app.
        await card_element.scroll_into_view_if_needed()
        await card_element.click()

        print("Clicked portal card, waiting for new tab with reports...")

        # Wait for a new page (tab) to open.
        new_page_promise = context.wait_for_event("page", timeout=60_000)
        try:
            new_page = await new_page_promise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("No new tab opened after clicking portal card.") from exc

        await new_page.wait_for_load_state("domcontentloaded")
        print("New reports tab detected. URL:", new_page.url)
        # Wait for reports page to fully load before interacting.
        try:
            await new_page.wait_for_load_state("networkidle", timeout=60_000)
        except PlaywrightTimeoutError:
            await new_page.wait_for_load_state("load")
        await asyncio.sleep(5)

        # Run Revenue in Depth Analysis (skip if already exported).
        import glob
        if not glob.glob(os.path.join(download_dir, "*Revenue-In-Depth.xlsx")):
            await run_in_depth_flow(
                new_page, download_dir,
                "Revenue in Depth Analysis",
                "Revenue-In-Depth",
                "header#CwPV_title",
            )
        else:
            print("Revenue-In-Depth already exported. Skipping.")

        # Close the reports tab and return to portal tab.
        print("Closing reports tab and returning to portal...")
        await new_page.close()
        await page.bring_to_front()
        await asyncio.sleep(2)

        # Click portal card again to open a new reports tab for Access In Depth.
        print("Clicking portal card again for Access In Depth...")
        await card_element.scroll_into_view_if_needed()
        await card_element.click()

        # Wait for new reports tab.
        new_page_promise = context.wait_for_event("page", timeout=60_000)
        try:
            new_page = await new_page_promise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("No new tab opened after clicking portal card (second time).") from exc

        await new_page.wait_for_load_state("domcontentloaded")
        print("New reports tab detected for Access In Depth. URL:", new_page.url)
        try:
            await new_page.wait_for_load_state("networkidle", timeout=60_000)
        except PlaywrightTimeoutError:
            await new_page.wait_for_load_state("load")
        await asyncio.sleep(5)

        # Run Access In Depth Analysis (skip if already exported).
        if not glob.glob(os.path.join(download_dir, "*Access-In-Depth.xlsx")):
            await run_in_depth_flow(
                new_page, download_dir,
                "Access in Depth Analysis",
                "Access-In-Depth",
                "header#b8cfe7c0-f39f-45ce-8529-5edd3499c57b_title",
            )
        else:
            print("Access-In-Depth already exported. Skipping.")

        # Close reports tab and return to portal for System Event In Depth.
        print("Closing reports tab and returning to portal...")
        await new_page.close()
        await page.bring_to_front()
        await asyncio.sleep(2)

        # Click portal card again for System Event In Depth.
        print("Clicking portal card again for System Event In Depth...")
        await card_element.scroll_into_view_if_needed()
        await card_element.click()

        new_page_promise = context.wait_for_event("page", timeout=60_000)
        try:
            new_page = await new_page_promise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("No new tab opened after clicking portal card (third time).") from exc

        await new_page.wait_for_load_state("domcontentloaded")
        print("New reports tab detected for System Event In Depth. URL:", new_page.url)
        try:
            await new_page.wait_for_load_state("networkidle", timeout=60_000)
        except PlaywrightTimeoutError:
            await new_page.wait_for_load_state("load")
        await asyncio.sleep(5)

        # Run System Event In Depth Analysis (skip if already exported).
        if not glob.glob(os.path.join(download_dir, "*System-Event-In-Depth.xlsx")):
            await run_in_depth_flow(
                new_page, download_dir,
                "System Event in Depth Analysis",
                "System-Event-In-Depth",
                "header#jKmZn_title",
            )
        else:
            print("System-Event-In-Depth already exported. Skipping.")

        # Close reports tab and return to portal for Parking Transactions.
        print("Closing reports tab and returning to portal...")
        await new_page.close()
        await page.bring_to_front()
        await asyncio.sleep(2)

        # Click portal card again for Parking Transactions.
        print("Clicking portal card again for Parking Transactions...")
        await card_element.scroll_into_view_if_needed()
        await card_element.click()

        new_page_promise = context.wait_for_event("page", timeout=60_000)
        try:
            new_page = await new_page_promise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "No new tab opened after clicking portal card (fourth time)."
            ) from exc

        await new_page.wait_for_load_state("domcontentloaded")
        print("New reports tab detected for Parking Transactions. URL:", new_page.url)
        try:
            await new_page.wait_for_load_state("networkidle", timeout=60_000)
        except PlaywrightTimeoutError:
            await new_page.wait_for_load_state("load")
        await asyncio.sleep(5)

        # Run Parking Transactions.
        await run_parking_transactions_flow(new_page, download_dir)

        await asyncio.sleep(3)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(login_and_open_portal())

