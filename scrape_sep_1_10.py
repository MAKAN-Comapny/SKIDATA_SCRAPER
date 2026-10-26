"""
SKIDATA September 1-10 Parking Transactions Scraper & Odoo Generator
-------------------------------------------------------------------
Task 1:
1. Logs into SKIDATA portal and navigates directly to 'Parking Transactions'.
2. Exports Parking Transactions day-by-day for September 1 to September 10, 2026.
3. Saves raw files to 'exports_sep_1_10/'.
4. Transforms each file using Odoo business logic (Payment != 0, Car Park mapping, Analytics Distribution, etc.).
5. Saves transformed files to 'exports_sep_1_10/' as *-Odoo.xlsx.
6. Uploads all 20 files directly to SharePoint folder 'Odoo_1-10_Sep_Testing' (if credentials provided).
"""
import asyncio
import os
import re
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

EXPORTS_DIR = "exports_sep_1_10"
TARGET_SHAREPOINT_FOLDER = "Odoo_1-10_Sep_Testing"


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_row_date(text: str) -> Optional[date]:
    """Parse Qlik date like '09/01/2026'."""
    try:
        return datetime.strptime(text.strip().split()[0], "%m/%d/%Y").date()
    except (ValueError, IndexError):
        return None


async def find_frame_with_selector(
    page: Page,
    selector: str,
    per_frame_timeout_ms: int = 15_000,
    total_timeout_ms: Optional[int] = None,
) -> Optional[Frame]:
    """Search all frames for one that contains the given selector."""
    if total_timeout_ms is None:
        total_timeout_ms = max(60_000, per_frame_timeout_ms * 2)
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


async def select_single_target_date(
    page: Page,
    list_frame: Frame,
    listbox_base: str,
    target_date: date,
) -> None:
    """Scroll down until target_date is visible in the Qlik Date listbox, then click it."""
    print(f"  Searching for date: {target_date.strftime('%d-%b-%Y')} ({target_date.strftime('%m/%d/%Y')})...")
    row_item = None
    seen = set()

    for scroll_idx in range(40):
        items = await list_frame.query_selector_all(listbox_base)
        for item in items:
            text = await item.inner_text()
            d = _parse_row_date(text)
            if d is not None:
                seen.add(d)
            if d == target_date:
                row_item = item
                break
        if row_item is not None:
            break

        if items:
            try:
                await items[-1].scroll_into_view_if_needed()
            except Exception:
                pass
        await asyncio.sleep(0.4)

    if row_item is None:
        seen_str = f"(seen dates: {min(seen)} to {max(seen)})" if seen else "(no dates seen)"
        raise RuntimeError(f"Could not find date {target_date} in Qlik Date listbox {seen_str}.")

    row_el = await row_item.query_selector('[role="row"]')
    already = row_el is not None and await row_el.get_attribute("aria-selected") == "true"
    if not already:
        await row_item.scroll_into_view_if_needed()
        await row_item.click()
        await asyncio.sleep(0.3)

    # Confirm selection
    confirm_button_selector = "button[data-testid='actions-toolbar-confirm'][title='Confirm selection']"
    confirm_frame = await find_frame_with_selector(page, confirm_button_selector, total_timeout_ms=5_000)
    if confirm_frame:
        await confirm_frame.click(confirm_button_selector)
        await asyncio.sleep(1.5)


async def clear_date_selection(page: Page) -> None:
    """Clear active date selection in Qlik."""
    try:
        for sel in [
            "button[data-testid='actions-toolbar-clear']",
            "button[title='Clear selection']",
            "div.qv-subtoolbar-button:has-text('Date') button.lui-icon--close",
            "button[title='Clear all selections']",
            "span.lui-icon--clear-selections",
        ]:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await asyncio.sleep(0.5)
                break
    except Exception:
        pass


async def wait_for_qlik_idle(page: Page) -> None:
    """Wait for Qlik calculation spinners and blockers to disappear."""
    try:
        await page.wait_for_selector(".qv-ui-blocker, .rain.progress, .cancel-overlay", state="hidden", timeout=45_000)
    except Exception:
        pass
    await asyncio.sleep(2)


async def export_current_parking_view(page: Page, download_path: str) -> None:
    """Right-click Parking Transactions header, click Download as Data, and save file."""
    await wait_for_qlik_idle(page)

    header_selector = "header#VvpsUS_title"
    pivot_frame = await find_frame_with_selector(page, header_selector, total_timeout_ms=25_000, per_frame_timeout_ms=3_000)
    if not pivot_frame:
        pivot_frame = await find_frame_with_selector(
            page, "article:not(.qv-object-filterpane) header.qv-object-header", total_timeout_ms=25_000, per_frame_timeout_ms=3_000
        )
    header_el = await pivot_frame.query_selector(header_selector)
    if not header_el:
        header_el = await pivot_frame.query_selector("article:not(.qv-object-filterpane) header.qv-object-header")
    await header_el.scroll_into_view_if_needed()
    await header_el.click(button="right", force=True)
    await asyncio.sleep(1.0)

    # Download as -> Data
    export_group_selector = "li#export-group"
    menu_frame = await find_frame_with_selector(page, export_group_selector, total_timeout_ms=10_000, per_frame_timeout_ms=2_000)
    await menu_frame.click(export_group_selector)
    await asyncio.sleep(0.8)

    export_selector = "li#export"
    export_item = await menu_frame.wait_for_selector(export_selector, timeout=10_000)
    await export_item.click()
    await asyncio.sleep(1.5)

    # Click export button in modal if present
    export_button_selector = "button.lui-button:has-text('Export'), button:has-text('Export')"
    export_button_frame = await find_frame_with_selector(page, export_button_selector, total_timeout_ms=10_000, per_frame_timeout_ms=2_000)
    if export_button_frame:
        btn = await export_button_frame.query_selector(export_button_selector)
        if btn:
            await btn.click()
            print("  Clicked Export modal button, waiting for generation...")

    await asyncio.sleep(10)

    # Wait for download link
    export_link_selector = "a.export-url"
    export_frame = await find_frame_with_selector(page, export_link_selector, total_timeout_ms=300_000, per_frame_timeout_ms=2_000)
    if not export_frame:
        await page.screenshot(path="debug_export_state.png")
        raise RuntimeError("Export link not found for Parking Transactions. Saved debug_export_state.png.")

    async with page.expect_download() as download_info:
        await export_frame.click(export_link_selector)
    download = await download_info.value

    # Close modal
    close_btn = await export_frame.query_selector("button:has-text('Close')")
    if close_btn:
        await close_btn.click()

    os.makedirs(os.path.dirname(download_path), exist_ok=True)
    await download.save_as(download_path)
    print(f"  Saved raw file: {os.path.basename(download_path)} ({os.path.getsize(download_path):,} bytes)")


async def scrape_sep_1_10():
    login_url = get_env("SKIDATA_URL", LOGIN_URL_DEFAULT)
    tenant = get_env("SKIDATA_TENANT")
    login_name = get_env("SKIDATA_LOGIN")
    password = get_env("SKIDATA_PASSWORD")
    headless = os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")

    os.makedirs(EXPORTS_DIR, exist_ok=True)

    # Dates to fetch: September 10 down to September 1 (chronological descending matches listbox)
    target_dates = [date(2026, 9, day) for day in range(10, 0, -1)]

    print("=" * 60)
    print("STEP 1: Scraping Parking Transactions for 1-10 September 2026")
    print(f"Target Dates: {len(target_dates)} days (from 10-Sep down to 01-Sep)")
    print(f"Output Directory: {os.path.abspath(EXPORTS_DIR)}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        print(f"Connecting to SKIDATA portal ({login_name} @ {tenant})...")
        await page.goto(login_url, wait_until="domcontentloaded")

        await page.fill("#tenantName", tenant)
        await page.fill("#loginName", login_name)
        await page.fill("#password", password)
        await page.click("form.login_form button[type='submit']")

        print("Logged in, waiting for portal card...")
        card_container_selector = "div.qv-content-li-inner:has-text('Parking_Dashboards_SA_Makan_Analyze')"
        await page.wait_for_load_state("networkidle")

        target_frame = None
        for frame in page.frames:
            try:
                await frame.wait_for_selector(card_container_selector, timeout=20_000)
                target_frame = frame
                break
            except PlaywrightTimeoutError:
                continue

        if target_frame is None:
            raise RuntimeError("Could not find portal card in any frame.")

        card_element = await target_frame.query_selector(card_container_selector)
        await card_element.scroll_into_view_if_needed()

        new_page_promise = context.wait_for_event("page", timeout=60_000)
        await card_element.click()
        reports_page = await new_page_promise
        await reports_page.wait_for_load_state("domcontentloaded")
        print("Reports tab opened. Waiting for load state...")
        await reports_page.wait_for_load_state("networkidle", timeout=60_000)
        await asyncio.sleep(4)

        # Open Parking Transactions sheet
        sheet_selector = "div.qv-content-li-inner:has(span[title='Parking Transactions'])"
        sheet_frame = await find_frame_with_selector(reports_page, sheet_selector)
        if not sheet_frame:
            raise RuntimeError("Could not find 'Parking Transactions' sheet card.")
        sheet_card = await sheet_frame.query_selector(sheet_selector)
        await sheet_card.scroll_into_view_if_needed()
        await sheet_card.click()
        print("Opened 'Parking Transactions' sheet.")
        await asyncio.sleep(4)

        listbox_base = "div.RowColumn-barContainer[data-testid='listbox.item']"

        for idx, target_date in enumerate(target_dates, 1):
            date_prefix = target_date.strftime("%d-%b-%y")
            dest_file = os.path.join(EXPORTS_DIR, f"{date_prefix}-Parking-Transactions.xlsx")

            if os.path.exists(dest_file) and os.path.getsize(dest_file) > 10_000:
                print(f"[{idx}/10] {date_prefix} already exists locally ({os.path.getsize(dest_file):,} bytes). Skipping download.")
                continue

            print(f"\n[{idx}/10] Exporting {date_prefix} ({target_date})...")

            # Open Date filter dropdown
            date_filter_selector = "div[data-testid='collapsed-title-Date']"
            date_frame = await find_frame_with_selector(reports_page, date_filter_selector, total_timeout_ms=15_000)
            if not date_frame:
                date_filter_selector = "h6[aria-label='Date']"
                date_frame = await find_frame_with_selector(reports_page, date_filter_selector, total_timeout_ms=15_000)
            await date_frame.click(date_filter_selector)
            await asyncio.sleep(0.8)

            list_frame = await find_frame_with_selector(reports_page, listbox_base, total_timeout_ms=15_000)

            # Select this specific date
            await select_single_target_date(reports_page, list_frame, listbox_base, target_date)

            # Export table
            await export_current_parking_view(reports_page, dest_file)

            # Clear date selection for the next day
            await clear_date_selection(reports_page)
            await asyncio.sleep(1)

        print("\n" + "=" * 60)
        print("ALL 10 DAYS DOWNLOADED SUCCESSFULLY!")
        print("=" * 60)
        await browser.close()


def transform_to_odoo():
    from odoo_transformer import transform_parking_file
    import glob

    print("\n" + "=" * 60)
    print("STEP 2: Transforming 1-10 September Raw Files into Odoo Invoices...")
    print("=" * 60)

    raw_files = sorted(glob.glob(os.path.join(EXPORTS_DIR, "*-Parking-Transactions.xlsx")))
    raw_files = [f for f in raw_files if not f.endswith("-Odoo.xlsx")]

    if not raw_files:
        raise RuntimeError(f"No raw files found in {EXPORTS_DIR} to transform.")

    for idx, src in enumerate(raw_files, 1):
        dest = src.replace(".xlsx", "-Odoo.xlsx")
        print(f"[{idx}/{len(raw_files)}] Transforming: {os.path.basename(src)} -> {os.path.basename(dest)}")
        before, after = transform_parking_file(src, dest)
        print(f"     Rows before: {before:,} | Rows kept (Payment != 0): {after:,}")

    print("\n" + "=" * 60)
    print("ALL 10 ODOO FILES SUCCESSFULLY GENERATED!")
    print("=" * 60)


def upload_to_sharepoint():
    import glob
    from To_Sharepoint import get_access_token, get_site_id, ensure_folder_path, upload_file

    print("\n" + "=" * 60)
    print(f"STEP 3: Uploading all 1-10 Sep files to SharePoint ({TARGET_SHAREPOINT_FOLDER})...")
    print("=" * 60)

    tenant_id = os.getenv("TENANT_ID")
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    site_name = os.getenv("SHAREPOINT_SITE_NAME")

    if not all([tenant_id, client_id, client_secret, site_name]):
        print("WARNING: SharePoint credentials (TENANT_ID / CLIENT_ID / CLIENT_SECRET) not set in .env.")
        print("Files are safely stored locally in: " + os.path.abspath(EXPORTS_DIR))
        print("Please fill them in .env, then rerun upload.")
        return

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    site_id = get_site_id(headers)

    ensure_folder_path(site_id, headers, TARGET_SHAREPOINT_FOLDER)

    all_files = sorted(glob.glob(os.path.join(EXPORTS_DIR, "*.xlsx")))
    print(f"Found {len(all_files)} files to upload to '{TARGET_SHAREPOINT_FOLDER}'.")

    for idx, fp in enumerate(all_files, 1):
        fn = os.path.basename(fp)
        print(f"[{idx}/{len(all_files)}] Uploading {fn} ({os.path.getsize(fp):,} bytes)...")
        res = upload_file(site_id, headers, fp, folder=TARGET_SHAREPOINT_FOLDER)
        print(f"     Uploaded OK -> {res.get('webUrl', 'Done')}")

    print("\n" + "=" * 60)
    print(f"ALL FILES UPLOADED TO SHAREPOINT: {TARGET_SHAREPOINT_FOLDER}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(scrape_sep_1_10())
    transform_to_odoo()
    upload_to_sharepoint()
