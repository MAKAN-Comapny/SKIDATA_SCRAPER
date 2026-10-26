"""
SKIDATA Historical Backfill Scraper
-----------------------------------
Downloads historical date ranges or full calendar months (e.g. August 2026, September 2026).
Extracts all 4 core sheets:
  1. Access In Depth Analysis
  2. Parking Transactions
  3. Revenue In Depth Analysis
  4. System Event In Depth Analysis

Outputs files formatted as:
  <start_day>-<month>-<year>-to-<end_day>-<month>-<year>-<sheet_name>.xlsx
  Matching SharePoint reference (e.g. 01-Jul-26-to-31-Jul-26-Parking-Transactions.xlsx).
"""
import argparse
import asyncio
import os
import sys
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

load_dotenv()
sys.stdout.reconfigure(line_buffering=True)

LOGIN_URL_DEFAULT = (
    "https://car.webhost.skidata.com/analytics/"
    "?proxyRestUri=https%3a%2f%2faut-qlikc01n01.sdnsa.net%3a4243%2fqps%2fswebanalyze4analytics%2f"
    "&targetId=5778d56a-9b37-4797-b185-736ebc571f4c"
)

EXPORTS_DIR = "exports_backfill"


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable or default: {name}")
    return value


def _parse_row_date(text: str) -> Optional[date]:
    """Parse Qlik date like '09/01/2026' or '08/31/2026'."""
    try:
        return datetime.strptime(text.strip().split()[0], "%m/%d/%Y").date()
    except (ValueError, IndexError):
        return None


async def find_frame_with_selector(
    page: Page,
    selector: str,
    per_frame_timeout_ms: int = 30_000,
    total_timeout_ms: Optional[int] = None,
) -> Optional[Frame]:
    """Search all frames for one that contains the given selector."""
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


async def select_calendar_month_range(
    page: Page,
    list_frame: Frame,
    listbox_base: str,
    start_date: date,
    end_date: date,
) -> int:
    """
    Scrolls through Qlik date listbox and selects every day between start_date and end_date (inclusive).
    """
    print(f"Selecting date range in listbox: {start_date} to {end_date}...")
    selected_count = 0
    seen_dates = set()

    # Dates in listbox are ordered descending (today -> past).
    # Scroll through until we see dates older than start_date.
    for scroll_idx in range(60):
        items = await list_frame.query_selector_all(listbox_base)
        new_items_found = False

        for item in items:
            text = await item.inner_text()
            d = _parse_row_date(text)
            if d is None:
                continue

            if d not in seen_dates:
                seen_dates.add(d)
                new_items_found = True

            if start_date <= d <= end_date:
                row_el = await item.query_selector('[role="row"]')
                is_selected = row_el is not None and await row_el.get_attribute("aria-selected") == "true"
                if not is_selected:
                    try:
                        await item.scroll_into_view_if_needed()
                        await item.click()
                        selected_count += 1
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass

        # If oldest seen date is already before start_date, we have covered the entire range
        if seen_dates and min(seen_dates) < start_date:
            break

        # Scroll down in virtual listbox
        try:
            if items:
                await items[-1].scroll_into_view_if_needed()
        except Exception:
            pass
        await list_frame.eval_on_selector(
            "div.RowColumn-scrollContainer, div.lui-virtual-scroll, div.lui-list",
            "el => el.scrollBy(0, 300)"
        )
        await asyncio.sleep(0.4)

    print(f"Selected {selected_count} day(s) for range {start_date} -> {end_date}.")

    # Confirm selection
    confirm_button_selector = "button[data-testid='actions-toolbar-confirm'][title='Confirm selection']"
    confirm_frame = await find_frame_with_selector(page, confirm_button_selector, per_frame_timeout_ms=5000)
    if confirm_frame is not None:
        await confirm_frame.click(confirm_button_selector)
    else:
        await page.keyboard.press("Enter")
    await asyncio.sleep(2)
    return selected_count


async def run_in_depth_flow(
    page: Page,
    download_dir: str,
    sheet_display_name: str,
    file_base_name: str,
    header_selector: str,
    start_date: date,
    end_date: date,
    file_prefix: str,
) -> None:
    """Open in-depth sheet, apply date filter, expand all, and export."""
    target_path = os.path.join(download_dir, f"{file_prefix}-{file_base_name}.xlsx")
    if os.path.exists(target_path) and os.path.getsize(target_path) > 10_000:
        print(f"Skipping {os.path.basename(target_path)} (already exists: {os.path.getsize(target_path):,} bytes).")
        return

    await page.evaluate("window.scrollTo(0, 0)")
    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except PlaywrightTimeoutError:
        await page.wait_for_load_state("load")
    await asyncio.sleep(3)

    # 1) Open sheet card
    sheet_selector_span = f"div.qv-content-li-inner:has(span[title='{sheet_display_name}'])"
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
        raise RuntimeError(f"Could not find '{sheet_display_name}' sheet card.")

    sheet_card = await sheet_frame.query_selector(sheet_selector)
    if sheet_card is None:
        raise RuntimeError("Sheet card element not found.")
    await sheet_card.scroll_into_view_if_needed()
    await sheet_card.click()
    print(f"Opened '{sheet_display_name}' sheet.")

    # 2) Date filter button
    date_button_selector = "div.lui-button.qv-pt-meta-button:has(.meta-text[title='Date'])"
    date_frame = await find_frame_with_selector(page, date_button_selector)
    if date_frame is None:
        raise RuntimeError("Could not find Date filter button.")
    await date_frame.click(date_button_selector)
    await asyncio.sleep(1.0)

    listbox_base = "div.RowColumn-barContainer[data-testid='listbox.item']"
    list_frame = await find_frame_with_selector(page, listbox_base)
    if list_frame is None:
        raise RuntimeError("Date listbox not found.")

    await select_calendar_month_range(page, list_frame, listbox_base, start_date, end_date)

    # 3) Expand all
    pivot_frame = await find_frame_with_selector(page, header_selector)
    if pivot_frame is None:
        raise RuntimeError(f"Could not find pivot header ({header_selector}).")
    pivot_header = await pivot_frame.query_selector(header_selector)
    await pivot_header.scroll_into_view_if_needed()
    await pivot_header.click(button="right")
    await asyncio.sleep(0.5)

    expand_group_selector = "li#expand-collapse-group"
    expand_all_selector = "li#expand-all"
    menu_frame = await find_frame_with_selector(page, expand_group_selector)
    if menu_frame is not None:
        await menu_frame.click(expand_group_selector)
        await asyncio.sleep(0.8)
        expand_all_el = await menu_frame.wait_for_selector(expand_all_selector, timeout=30_000)
        await expand_all_el.click()
        await asyncio.sleep(3)

    # 4) Export Data
    await pivot_header.click(button="right")
    await asyncio.sleep(0.5)

    export_group_selector = "li#export-group"
    export_selector = "li#export"
    export_frame_menu = await find_frame_with_selector(page, export_group_selector)
    await export_frame_menu.click(export_group_selector)
    await asyncio.sleep(0.5)

    export_item = await export_frame_menu.wait_for_selector(export_selector, timeout=60_000)
    await export_item.click()

    # 5) Wait for export link
    print("Waiting for export link...")
    export_link_selector = "a.export-url"
    export_frame = await find_frame_with_selector(page, export_link_selector, total_timeout_ms=300_000)
    if export_frame is None:
        raise RuntimeError("Export link did not appear in time.")

    os.makedirs(download_dir, exist_ok=True)
    async with page.expect_download(timeout=180_000) as download_info:
        await export_frame.click(export_link_selector)
    download = await download_info.value
    await download.save_as(target_path)
    print(f"Download complete: {target_path} ({os.path.getsize(target_path):,} bytes)")


async def run_parking_transactions_flow(
    page: Page,
    download_dir: str,
    start_date: date,
    end_date: date,
    file_prefix: str,
) -> None:
    """Open Parking Transactions sheet, apply date filter, and export data."""
    target_path = os.path.join(download_dir, f"{file_prefix}-Parking-Transactions.xlsx")
    if os.path.exists(target_path) and os.path.getsize(target_path) > 10_000:
        print(f"Skipping {os.path.basename(target_path)} (already exists: {os.path.getsize(target_path):,} bytes).")
        return

    sheet_display_name = "Parking Transactions"
    header_selector = "header#VvpsUS_title"

    await page.evaluate("window.scrollTo(0, 0)")
    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except PlaywrightTimeoutError:
        await page.wait_for_load_state("load")
    await asyncio.sleep(3)

    # 1) Open sheet card
    sheet_selector = "div.qv-content-li-inner:has(span[title='Parking Transactions'])"
    sheet_frame = await find_frame_with_selector(page, sheet_selector)
    if sheet_frame is None:
        raise RuntimeError(f"Could not find '{sheet_display_name}' sheet card.")
    sheet_card = await sheet_frame.query_selector(sheet_selector)
    await sheet_card.scroll_into_view_if_needed()
    await sheet_card.click()
    print(f"Opened '{sheet_display_name}' sheet.")

    # 2) Date filter in filter pane
    date_filter_selector = "div[data-testid='collapsed-title-Date']"
    date_frame = await find_frame_with_selector(page, date_filter_selector)
    if date_frame is None:
        date_filter_selector = "h6[aria-label='Date']"
        date_frame = await find_frame_with_selector(page, date_filter_selector)
    await date_frame.click(date_filter_selector)
    await asyncio.sleep(1.0)

    listbox_base = "div.RowColumn-barContainer[data-testid='listbox.item']"
    list_frame = await find_frame_with_selector(page, listbox_base)
    await select_calendar_month_range(page, list_frame, listbox_base, start_date, end_date)

    await asyncio.sleep(4)
    await page.evaluate("window.scrollTo(0, 300)")

    # 3) Export
    pivot_frame = await find_frame_with_selector(page, header_selector, per_frame_timeout_ms=60_000)
    if pivot_frame is None:
        pivot_frame = await find_frame_with_selector(
            page, "article:not(.qv-object-filterpane) header.qv-object-header", per_frame_timeout_ms=60_000
        )
    pivot_header = await pivot_frame.query_selector(header_selector) or await pivot_frame.query_selector("article:not(.qv-object-filterpane) header.qv-object-header")
    await pivot_header.scroll_into_view_if_needed()
    await pivot_header.click(button="right")
    await asyncio.sleep(0.5)

    export_group_selector = "li#export-group"
    export_selector = "li#export"
    export_frame_menu = await find_frame_with_selector(page, export_group_selector)
    await export_frame_menu.click(export_group_selector)
    await asyncio.sleep(0.5)

    export_item = await export_frame_menu.wait_for_selector(export_selector, timeout=60_000)
    await export_item.click()
    await asyncio.sleep(1.0)

    export_button_selector = "button.lui-button:has-text('Export')"
    export_button_frame = await find_frame_with_selector(page, export_button_selector, per_frame_timeout_ms=10_000)
    if export_button_frame is not None:
        btn = await export_button_frame.query_selector(export_button_selector)
        if btn is not None:
            await btn.click()

    # 4) Download link
    print("Waiting for Parking Transactions export...")
    export_link_selector = "a.export-url"
    export_frame = await find_frame_with_selector(page, export_link_selector, total_timeout_ms=300_000)
    if export_frame is None:
        raise RuntimeError("Export link did not appear in time.")

    os.makedirs(download_dir, exist_ok=True)
    async with page.expect_download(timeout=180_000) as download_info:
        await export_frame.click(export_link_selector)
    download = await download_info.value
    await download.save_as(target_path)
    print(f"Parking Transactions complete: {target_path} ({os.path.getsize(target_path):,} bytes)")


async def scrape_month(
    month_name: str,
    start_date: date,
    end_date: date,
    file_prefix: str,
    download_dir: str,
    sheet_filter: str = "all",
    headless: bool = True,
) -> None:
    """Logs into portal and downloads requested sheets for a given month."""
    skidata_url = get_env("SKIDATA_URL", LOGIN_URL_DEFAULT)
    tenant = get_env("SKIDATA_TENANT", "SA_Makan_Analyze")
    login = get_env("SKIDATA_LOGIN", "analyze2")
    password = get_env("SKIDATA_PASSWORD", "Skidata123456")

    print(f"\n" + "=" * 65)
    print(f"SCRAPING {month_name.upper()} ({start_date} to {end_date})")
    print(f"Prefix:      {file_prefix}")
    print(f"Sheets:      {sheet_filter}")
    print(f"Destination: {os.path.abspath(download_dir)}")
    print("=" * 65)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        print(f"Opening SKIDATA login page: {skidata_url}")
        await page.goto(skidata_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.fill("#tenantName", tenant)
        await page.fill("#loginName", login)
        await page.fill("#password", password)
        print("Submitting login form...")
        await page.click("form.login_form button[type='submit']")

        print("Waiting for portal hub to load (networkidle)...")
        try:
            await page.wait_for_load_state("networkidle", timeout=45_000)
        except PlaywrightTimeoutError:
            await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)

        card_container_selector = "div.qv-content-li-inner:has-text('Parking_Dashboards_SA_Makan_Analyze')"
        print("Searching for portal app card across all frames...")
        target_frame = await find_frame_with_selector(
            page, card_container_selector, per_frame_timeout_ms=15_000, total_timeout_ms=60_000
        )
        if target_frame is None:
            raise RuntimeError("Could not find portal app card in any frame after login.")
        print("Found portal app card.")

        async def open_reports_tab():
            card_frame = await find_frame_with_selector(
                page, card_container_selector, per_frame_timeout_ms=15_000, total_timeout_ms=60_000
            )
            if not card_frame:
                raise RuntimeError("Could not find portal card frame.")
            card_el = await card_frame.query_selector(card_container_selector)
            if not card_el:
                raise RuntimeError("Portal card element not found.")
            await card_el.scroll_into_view_if_needed()
            await asyncio.sleep(1)

            print("Clicking portal card to open reports tab...")
            async with context.expect_page(timeout=60_000) as new_page_info:
                await card_el.click(force=True)
            tab = await new_page_info.value
            await tab.wait_for_load_state("domcontentloaded")
            print(f"Reports tab opened: {tab.url}. Waiting for network idle...")
            try:
                await tab.wait_for_load_state("networkidle", timeout=60_000)
            except PlaywrightTimeoutError:
                await tab.wait_for_load_state("load")
            await asyncio.sleep(5)
            return tab

        # 1. Revenue In Depth
        if sheet_filter in ("all", "revenue"):
            tab = await open_reports_tab()
            await run_in_depth_flow(
                tab, download_dir, "Revenue in Depth Analysis", "Revenue-In-Depth",
                "header#CwPV_title", start_date, end_date, file_prefix
            )
            print("Closing reports tab and returning to portal...")
            await tab.close()
            await page.bring_to_front()
            await asyncio.sleep(2)

        # 2. Access In Depth
        if sheet_filter in ("all", "access"):
            tab = await open_reports_tab()
            await run_in_depth_flow(
                tab, download_dir, "Access in Depth Analysis", "Access-In-Depth",
                "header#b8cfe7c0-f39f-45ce-8529-5edd3499c57b_title", start_date, end_date, file_prefix
            )
            print("Closing reports tab and returning to portal...")
            await tab.close()
            await page.bring_to_front()
            await asyncio.sleep(2)

        # 3. System Event In Depth
        if sheet_filter in ("all", "system_events", "events"):
            tab = await open_reports_tab()
            await run_in_depth_flow(
                tab, download_dir, "System Event in Depth Analysis", "System-Event-In-Depth",
                "header#jKmZn_title", start_date, end_date, file_prefix
            )
            print("Closing reports tab and returning to portal...")
            await tab.close()
            await page.bring_to_front()
            await asyncio.sleep(2)

        # 4. Parking Transactions
        if sheet_filter in ("all", "parking", "transactions"):
            tab = await open_reports_tab()
            await run_parking_transactions_flow(
                tab, download_dir, start_date, end_date, file_prefix
            )
            print("Closing reports tab and returning to portal...")
            await tab.close()
            await page.bring_to_front()
            await asyncio.sleep(2)

        await browser.close()
        print(f"\nFinished scraping {month_name}.")
