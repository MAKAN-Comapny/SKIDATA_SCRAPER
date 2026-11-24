"""
sharepoint_reader.py - Downloads latest SkiData 15-day files from SharePoint 'Ski Data 5'.

Guarantees:
  1. Stale Data Guard: Enforces that downloaded files are fresh (today or yesterday AST).
     Prevents reconciling Fabric against outdated or un-fetched data if today's run has not landed.
  2. Automatic Download & Extraction: Downloads Revenue-In-Depth and Parking-Transactions into exports/.
  3. Seamless Transformation: Triggers Transformer.py to populate Cleaned-Exports/.
"""
from __future__ import annotations

import os
import re
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import requests
from dotenv import load_dotenv

load_dotenv()

from To_Sharepoint import get_access_token, get_site_id

logger = logging.getLogger("sharepoint_reader")
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AST_TIMEZONE = timezone(timedelta(hours=3))


def get_ast_now() -> datetime:
    """Returns current datetime in Saudi Arabia Standard Time (UTC+3)."""
    return datetime.now(AST_TIMEZONE)


def parse_date_from_filename(filename: str) -> Optional[date]:
    """
    Extracts date from filename, taking the last (end) date for range files.
    Supports formats:
      - '24-Nov-26-Parking-Transactions.xlsx' -> 2026-11-24
      - '24-Nov-26-12Noon-Parking-Transactions.xlsx' -> 2026-11-24
      - '10-Nov-26-To-24-Nov-26-Parking-Transactions.xlsx' -> 2026-11-24
      - '2026-11-24-Parking-Transactions.xlsx' -> 2026-11-24
      - '24-November-2026-Parking-Transactions.xlsx' -> 2026-11-24
    """
    matches = re.findall(r"(\d{1,2})[-_]([A-Za-z]{3,9})[-_](\d{2,4})", filename)
    if matches:
        day, mon, yr = matches[-1]
        for mon_fmt in ("%b", "%B"):
            yr_fmt = "%Y" if len(yr) == 4 else "%y"
            try:
                return datetime.strptime(f"{day}-{mon}-{yr}", f"%d-{mon_fmt}-{yr_fmt}").date()
            except ValueError:
                pass

    matches2 = re.findall(r"(\d{4})[-_](\d{1,2})[-_](\d{1,2})", filename)
    if matches2:
        yr, mon, day = matches2[-1]
        try:
            return date(int(yr), int(mon), int(day))
        except ValueError:
            pass

    matches3 = re.findall(r"(\d{1,2})[-_](\d{1,2})[-_](\d{4})", filename)
    if matches3:
        day, mon, yr = matches3[-1]
        try:
            return date(int(yr), int(mon), int(day))
        except ValueError:
            pass

    return None


def list_sharepoint_folder(folder_path: str = "Ski Data 5") -> List[dict]:
    """Lists all file items in specified SharePoint folder."""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    site_id = get_site_id(headers)

    encoded_path = "/".join(requests.utils.quote(part, safe="") for part in folder_path.split("/") if part)
    url = f"{GRAPH_BASE}/sites/{site_id}/drive/root:/{encoded_path}:/children"

    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        raise FileNotFoundError(f"SharePoint folder '{folder_path}' not found (404).")
    r.raise_for_status()
    items = r.json().get("value", [])
    return [i for i in items if "file" in i]


def download_latest_from_sharepoint(
    source_folder: str = "Ski Data 5",
    target_date: Optional[date] = None,
    allow_stale: bool = False,
    dest_dir: str = "exports",
) -> Dict[str, Any]:
    """
    Downloads latest Revenue-In-Depth and Parking-Transactions files from SharePoint.
    Strictly verifies freshness to ensure stale data is never reconciled.

    Args:
        source_folder: SharePoint folder name (e.g. 'Ski Data 5').
        target_date: Explicit target date (if user requested a specific day).
        allow_stale: If True, bypasses stale data guard for testing.
        dest_dir: Local directory to save downloaded raw Excel files.

    Returns:
        Dict with keys: 'revenue', 'parking', 'file_date', 'source_folder'.
    """
    logger.info(f"Connecting to SharePoint to fetch latest 15-day files from: [{source_folder}]...")
    items = list_sharepoint_folder(source_folder)
    if not items:
        raise FileNotFoundError(f"No files found in SharePoint folder '{source_folder}'.")

    markers = {
        "revenue": "Revenue-In-Depth",
        "parking": "Parking-Transactions",
    }
    selected_files: Dict[str, Any] = {"source_folder": source_folder}

    today_ast = get_ast_now().date()
    yesterday_ast = today_ast - timedelta(days=1)
    allowed_dates = {today_ast, yesterday_ast}
    if target_date:
        allowed_dates.add(target_date)

    discovered_dates: List[date] = []

    for category, marker in markers.items():
        candidates = [
            i for i in items
            if marker.lower() in i.get("name", "").lower()
            and i.get("name", "").lower().endswith((".xlsx", ".xlsm"))
            and "odoo" not in i.get("name", "").lower()
            and "reconciliation" not in i.get("name", "").lower()
        ]
        if not candidates:
            raise FileNotFoundError(f"No raw '{marker}' .xlsx file found in SharePoint folder '{source_folder}'.")

        # If an explicit target_date was requested, filter for matching candidates first
        if target_date:
            matching_candidates = [
                i for i in candidates
                if parse_date_from_filename(i.get("name", "")) == target_date
            ]
            if matching_candidates:
                candidates = matching_candidates
            elif not allow_stale and os.getenv("ALLOW_STALE_DATA", "").lower() not in ("true", "1", "yes"):
                raise FileNotFoundError(
                    f"STALE DATA GUARD: No '{marker}' file matching target date {target_date} was found in '{source_folder}'. "
                    f"Aborting reconciliation to avoid using an incorrect date."
                )

        # Sort newest by parsed date, then by lastModifiedDateTime
        def sort_key(item: dict):
            p_date = parse_date_from_filename(item.get("name", ""))
            p_date_str = p_date.isoformat() if p_date else "1970-01-01"
            mod_str = item.get("lastModifiedDateTime", "")
            return (p_date_str, mod_str)

        candidates.sort(key=sort_key, reverse=True)
        chosen = candidates[0]
        chosen_name = chosen["name"]
        file_date = parse_date_from_filename(chosen_name)
        if file_date:
            discovered_dates.append(file_date)

        logger.info(f"Selected SharePoint '{marker}' candidate: {chosen_name} (parsed date: {file_date})")

        # FRESHNESS GUARD: Prevent reconciling against stale data!
        if not allow_stale and os.getenv("ALLOW_STALE_DATA", "").lower() not in ("true", "1", "yes"):
            # Check 1: Target date mismatch
            if target_date and file_date and file_date != target_date:
                raise ValueError(
                    f"STALE DATA GUARD: Explicit target date is {target_date}, but latest file in '{source_folder}' is '{chosen_name}' ({file_date}). "
                    f"Target data has not been fetched yet! Aborting reconciliation to prevent inaccurate audit."
                )
            # Check 2: Filename date freshness (must be today or yesterday AST)
            if not target_date and file_date and file_date not in allowed_dates:
                days_old = (today_ast - file_date).days if file_date else 99
                raise ValueError(
                    f"STALE DATA GUARD: Latest file in SharePoint '{source_folder}' is '{chosen_name}' "
                    f"({file_date}, {days_old} days old). Expected today ({today_ast}) or yesterday ({yesterday_ast}) AST. "
                    f"Today's data has NOT been fetched yet! Aborting reconciliation to prevent inaccurate audit."
                )
            # Check 3: SharePoint file upload/modification freshness
            last_mod_str = chosen.get("lastModifiedDateTime")
            if not target_date and last_mod_str:
                try:
                    last_mod_dt = datetime.fromisoformat(last_mod_str.replace("Z", "+00:00")).astimezone(AST_TIMEZONE)
                    last_mod_date = last_mod_dt.date()
                    if last_mod_date not in allowed_dates:
                        days_old_mod = (today_ast - last_mod_date).days
                        raise ValueError(
                            f"STALE DATA GUARD: File '{chosen_name}' was uploaded/modified on {last_mod_date} AST "
                            f"({days_old_mod} days ago in SharePoint). Expected today ({today_ast}) or yesterday ({yesterday_ast}) AST. "
                            f"Today's data has NOT been fetched yet! Aborting reconciliation to prevent inaccurate audit."
                        )
                except (ValueError, TypeError) as parse_err:
                    logger.debug(f"Could not parse lastModifiedDateTime '{last_mod_str}': {parse_err}")

        # Download file
        download_url = chosen.get("@microsoft.graph.downloadUrl")
        if not download_url:
            raise RuntimeError(f"No download URL available for SharePoint file '{chosen_name}'.")

        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, chosen_name)
        logger.info(f"Downloading {chosen_name} ({chosen.get('size', 0):,} bytes)...")
        r_dl = requests.get(download_url, timeout=180)
        r_dl.raise_for_status()

        with open(dest_path, "wb") as f:
            f.write(r_dl.content)

        selected_files[category] = dest_path
        logger.info(f"✅ Downloaded to: {dest_path}")

    # Set reconciled file_date
    selected_files["file_date"] = discovered_dates[0] if discovered_dates else None

    # Run Transformer to populate Cleaned-Exports
    try:
        from Transformer import run as transform_exports
        logger.info("Transforming downloaded SharePoint exports into Cleaned-Exports/...")
        transform_exports()
    except Exception as e_trans:
        logger.warning(f"Transformer note: {e_trans}")

    return selected_files
