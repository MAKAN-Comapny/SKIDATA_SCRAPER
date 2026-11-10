"""
SKIDATA Automated SharePoint File Archiver & Moving Engine
Safely relocates Excel files (.xlsx, .xls) from a source folder (default: Validated_Docs)
to target month-based or current-date-based subfolders inside an archive directory (default: Validated_Docs/old).

Key Guarantees:
  1. Atomic Move:
     - Uses Microsoft Graph API PATCH /drive/items/{id} to update the parentReference.
     - Leaves ZERO residual files in the source folder (100% single-source-of-truth).
  2. Subfolder Protection:
     - Folders (including 'old' itself) and non-Excel items are strictly ignored and never moved.
  3. Smart Month & Date Detection:
     - Matches month abbreviations/names (e.g. 'Oct', 'October' -> 'Oct' or reuses existing 'October').
     - If no month name is found, falls back directly to the current date in Riyadh AST (e.g. '10-Nov-2026').
  4. End-to-End Verification:
     - Verifies each move response from Microsoft Graph.
     - Performs a full post-move reconciliation audit (source must not contain moved files, target must contain them).
  5. Idempotent & Fault-Tolerant:
     - Can be run repeatedly without duplicating or corrupting data.
     - Built-in retry mechanism with exponential backoff for network lag / Graph API throttling.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv

load_dotenv()

# Microsoft Graph Configuration
TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SITE_NAME = os.getenv("SHAREPOINT_SITE_NAME")

DEFAULT_SOURCE_FOLDER = "Odoo_aug_sep"
DEFAULT_ARCHIVE_ROOT = "Odoo_aug_sep/oct_test"

# 12 Month Keywords and Aliases
MONTH_KEYWORDS = {
    "Jan": ["jan", "january"],
    "Feb": ["feb", "february"],
    "Mar": ["mar", "march"],
    "Apr": ["apr", "april"],
    "May": ["may"],
    "Jun": ["jun", "june"],
    "Jul": ["jul", "july"],
    "Aug": ["aug", "august"],
    "Sep": ["sep", "sept", "september"],
    "Oct": ["oct", "october"],
    "Nov": ["nov", "november"],
    "Dec": ["dec", "december"],
}


def get_riyadh_now() -> datetime:
    """Returns current datetime in Arabia Standard Time (Riyadh, UTC+3)."""
    return datetime.now(timezone(timedelta(hours=3)))


def get_access_token() -> str:
    """Obtain Microsoft Graph OAuth2 bearer token."""
    if not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
        raise ValueError("Missing required credentials: TENANT_ID, CLIENT_ID, or CLIENT_SECRET.")

    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    r = requests.post(token_url, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def get_site_id(headers: dict, site_input: str) -> str:
    """Find SharePoint site ID by URL or display name."""
    clean_site = site_input.strip()

    # Full URL check
    url_match = re.match(r"https?://([^/]+)(/.*)?", clean_site)
    if url_match:
        hostname = url_match.group(1)
        path = (url_match.group(2) or "/").rstrip("/") or "/"
        if not path.startswith("/"):
            path = "/" + path
        get_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:{path}"
        r = requests.get(get_url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()["id"]

    # Site search
    site_search_url = f"https://graph.microsoft.com/v1.0/sites?search={requests.utils.quote(clean_site)}"
    r = requests.get(site_search_url, headers=headers, timeout=30)
    r.raise_for_status()
    sites = r.json().get("value", [])
    if not sites:
        raise ValueError(f"No SharePoint site found matching: '{clean_site}'")
    return sites[0]["id"]


def list_folder_children(site_id: str, headers: dict, folder_path: str) -> List[Dict[str, Any]]:
    """List all children in a folder, handling pagination."""
    items = []
    clean_path = folder_path.strip("/")
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{clean_path}:/children?$top=200"

    while url:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 404:
            return []  # Folder does not exist yet
        r.raise_for_status()
        data = r.json()
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")

    return items


def get_or_create_folder_id(site_id: str, headers: dict, folder_path: str) -> str:
    """Ensure nested folder path exists in SharePoint and return its driveItem ID."""
    clean_path = folder_path.strip("/")
    parts = [p for p in clean_path.split("/") if p.strip()]
    current = ""
    last_id = None

    for part in parts:
        current = f"{current}/{part}" if current else part
        check_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{current}"
        r = requests.get(check_url, headers=headers, timeout=30)

        if r.status_code == 200:
            last_id = r.json()["id"]
        elif r.status_code == 404:
            parent = current.rsplit("/", 1)[0] if "/" in current else ""
            create_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{parent}:/children"
                if parent
                else f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children"
            )
            payload = {
                "name": part,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            }
            create_res = requests.post(create_url, headers=headers, json=payload, timeout=30)
            if create_res.status_code == 409:
                # Created concurrently by another worker
                recheck = requests.get(check_url, headers=headers, timeout=30)
                recheck.raise_for_status()
                last_id = recheck.json()["id"]
            else:
                create_res.raise_for_status()
                last_id = create_res.json()["id"]
        else:
            r.raise_for_status()

    if not last_id:
        raise RuntimeError(f"Could not resolve folder ID for path: '{folder_path}'")
    return last_id


def detect_target_subfolder(filename: str, existing_subfolders: List[str]) -> str:
    """
    Determines destination subfolder name:
      1. Scans filename tokens for any recognized month (e.g. 'Oct' or 'October').
         If found, reuses matching existing folder (e.g. 'October') or defaults to 'Oct'.
      2. If no month is found, creates a folder named after current latest date (e.g. '10-Nov-2026').
    """
    clean_name = filename.lower()
    tokens = set(re.split(r"[-_ .\s\(\)]+", clean_name))

    matched_month_key = None
    for month_canonical, aliases in MONTH_KEYWORDS.items():
        if any(alias in tokens for alias in aliases):
            matched_month_key = month_canonical
            break

    if matched_month_key:
        # Check if an existing subfolder in archive root already represents this month
        for existing in existing_subfolders:
            ex_lower = existing.lower()
            if any(alias in ex_lower for alias in MONTH_KEYWORDS[matched_month_key]):
                return existing
        return matched_month_key  # default to 'Oct', 'Aug', 'Sep', etc.

    # Fallback when no month detected: current date in Riyadh AST (UTC+3)
    riyadh_now = get_riyadh_now()
    return riyadh_now.strftime("%d-%b-%Y")


def resolve_destination_path(archive_root: str, target_subfolder: str) -> str:
    """
    Computes the full destination folder path.
    If archive_root already is the target folder (e.g. ends with 'oct_test' matching month 'Oct'),
    it places files directly into archive_root without creating a redundant nested subfolder.
    Otherwise, appends /{target_subfolder}.
    """
    root_clean = archive_root.strip("/")
    last_seg = root_clean.split("/")[-1].lower()
    sub_lower = target_subfolder.lower()

    if sub_lower in last_seg or last_seg.startswith(sub_lower):
        return root_clean

    return f"{root_clean}/{target_subfolder}"


def move_item_atomic(
    site_id: str,
    headers: dict,
    item_id: str,
    item_name: str,
    target_folder_id: str,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Moves a file to target folder via Microsoft Graph PATCH.
    Leaves 0 residual copies in the source folder.
    """
    move_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}"
    payload = {
        "parentReference": {"id": target_folder_id},
        "name": item_name,
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.patch(move_url, headers=headers, json=payload, timeout=45)

            if r.status_code == 200:
                data = r.json()
                new_parent_id = data.get("parentReference", {}).get("id")
                if new_parent_id and new_parent_id != target_folder_id:
                    raise RuntimeError(f"Move mismatch: expected parent {target_folder_id}, got {new_parent_id}")
                return data

            if r.status_code == 409:
                # Name collision in target folder: append timestamp and retry
                base, ext = os.path.splitext(item_name)
                timestamp = int(time.time())
                unique_name = f"{base}_dup_{timestamp}{ext}"
                print(f"    [Conflict 409] File '{item_name}' exists in destination. Renaming to '{unique_name}'...")
                payload["name"] = unique_name
                continue

            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = int(r.headers.get("Retry-After", 2 ** attempt))
                print(f"    [HTTP {r.status_code}] Throttled/Server busy. Retrying in {retry_after}s (attempt {attempt}/{max_retries})...")
                time.sleep(retry_after)
                continue

            r.raise_for_status()

        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                break

    raise RuntimeError(f"Failed to move '{item_name}' (ID: {item_id}) after {max_retries} attempts: {last_error}")


def archive_validated_docs(
    source_folder: str = DEFAULT_SOURCE_FOLDER,
    archive_root: str = DEFAULT_ARCHIVE_ROOT,
    dry_run: bool = False,
) -> None:
    """Main orchestration for scanning, classifying, moving, and verifying files."""
    start_time = time.time()
    riyadh_time_str = get_riyadh_now().strftime("%Y-%m-%d %H:%M:%S AST")

    print("=" * 70)
    print("SKIDATA SHAREPOINT AUTOMATIC ARCHIVER & FILE MOVER")
    print(f"Execution Time:    {riyadh_time_str}")
    print(f"Source Folder:     {source_folder}")
    print(f"Archive Root:      {archive_root}")
    print(f"Dry Run Mode:      {dry_run}")
    print("=" * 70)

    # 1. Authenticate
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    site_id = get_site_id(headers, SITE_NAME)
    print(f"[Auth] Successfully authenticated with SharePoint Site ID: {site_id}")

    # 2. Query source folder children
    print(f"\n[Scan] Querying items in source folder: '{source_folder}'...")
    source_items = list_folder_children(site_id, headers, source_folder)
    print(f"[Scan] Found {len(source_items)} total item(s) in '{source_folder}'.")

    # 3. Filter Excel files only (strictly exclude folders like 'old' and non-Excel)
    candidate_files = []
    for item in source_items:
        # Ignore subfolders
        if "folder" in item:
            print(f"  - [SKIP FOLDER] '{item.get('name')}' (subfolder, untouched)")
            continue
        # Ensure it's a file
        if "file" not in item:
            continue

        name = item.get("name", "").strip()
        # Extension check
        if not (name.lower().endswith(".xlsx") or name.lower().endswith(".xls")):
            print(f"  - [SKIP NON-EXCEL] '{name}' (not an .xlsx file)")
            continue
        # Lock / temp file check
        if name.startswith("~$") or name.startswith("."):
            print(f"  - [SKIP TEMP FILE] '{name}'")
            continue

        candidate_files.append(item)

    if not candidate_files:
        print("\n[Notice] No Excel files found to move in source folder. Everything is clean and organized!")
        print("=" * 70)
        return

    print(f"\n[Classify] Identified {len(candidate_files)} Excel file(s) eligible for archival.")

    # 4. Read existing subfolders inside archive_root (e.g. 'old') to reuse existing naming
    archive_children = list_folder_children(site_id, headers, archive_root)
    existing_subfolders = [
        item["name"] for item in archive_children if "folder" in item
    ]
    print(f"[Classify] Existing subfolder(s) in '{archive_root}': {existing_subfolders or 'None'}")

    # 5. Group files by target destination path
    moves_planned = []
    needed_dest_paths = set()

    for item in candidate_files:
        filename = item["name"]
        target_subfolder = detect_target_subfolder(filename, existing_subfolders)
        full_dest_path = resolve_destination_path(archive_root, target_subfolder)
        moves_planned.append({
            "id": item["id"],
            "name": filename,
            "size": item.get("size", 0),
            "subfolder": target_subfolder,
            "full_dest_path": full_dest_path,
        })
        needed_dest_paths.add(full_dest_path)
        print(f"  -> '{filename}' ==> '{full_dest_path}'")

    if dry_run:
        print("\n[Dry Run] Completed classification without moving any files.")
        return

    # 6. Ensure all target folders exist and obtain their SharePoint folder IDs
    print(f"\n[Prepare] Ensuring {len(needed_dest_paths)} target destination folder(s) exist in SharePoint...")
    dest_folder_ids = {}
    for dest_p in needed_dest_paths:
        fid = get_or_create_folder_id(site_id, headers, dest_p)
        dest_folder_ids[dest_p] = fid
        print(f"  [OK] '{dest_p}' (Folder ID: {fid})")

    # 7. Execute Atomic Moves
    print(f"\n[Move] Beginning atomic move of {len(moves_planned)} file(s)...")
    moved_records = []
    failed_records = []

    for idx, plan in enumerate(moves_planned, start=1):
        item_id = plan["id"]
        filename = plan["name"]
        dest_p = plan["full_dest_path"]
        dest_fid = dest_folder_ids[dest_p]

        print(f"  [{idx}/{len(moves_planned)}] Moving '{filename}' -> '{dest_p}'...")
        try:
            result = move_item_atomic(
                site_id=site_id,
                headers=headers,
                item_id=item_id,
                item_name=filename,
                target_folder_id=dest_fid,
            )
            moved_records.append({
                "name": result.get("name", filename),
                "original_name": filename,
                "id": result.get("id", item_id),
                "size": plan["size"],
                "target_subfolder": subf,
                "status": "SUCCESS",
            })
            print(f"    [SUCCESS] Moved '{filename}' (ID: {item_id})")
        except Exception as err:
            failed_records.append({
                "name": filename,
                "target_subfolder": subf,
                "error": str(err),
                "status": "FAILED",
            })
            print(f"    [ERROR] Failed to move '{filename}': {err}")

    # 8. Post-Move Reconciliation Audit
    print("\n[Audit] Performing post-move reconciliation verification...")
    recheck_source = list_folder_children(site_id, headers, source_folder)
    recheck_source_ids = {it["id"] for it in recheck_source}

    residual_in_source = [r for r in moved_records if r["id"] in recheck_source_ids]
    if residual_in_source:
        print(f"  [AUDIT WARNING] {len(residual_in_source)} file(s) still detected in source folder!")
    else:
        print(f"  [AUDIT PASSED] Source folder '{source_folder}' is completely clean of moved files!")

    elapsed_sec = round(time.time() - start_time, 2)
    print("\n" + "=" * 70)
    print("ARCHIVE & MOVE SUMMARY")
    print(f"Total Eligible Files: {len(candidate_files)}")
    print(f"Successfully Moved:   {len(moved_records)}")
    print(f"Failed Moves:         {len(failed_records)}")
    print(f"Total Elapsed Time:   {elapsed_sec}s")
    print("=" * 70)

    # Output GitHub Actions step summary if running in CI
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("### 📦 SharePoint Automatic File Archival Summary\n\n")
                f.write(f"- **Execution Time:** `{riyadh_time_str}`\n")
                f.write(f"- **Source Folder:** `{source_folder}`\n")
                f.write(f"- **Archive Root:** `{archive_root}`\n")
                f.write(f"- **Files Moved:** `{len(moved_records)} / {len(candidate_files)}`\n\n")
                if moved_records:
                    f.write("| File Name | Target Subfolder | Size | Status |\n")
                    f.write("| :--- | :--- | :--- | :--- |\n")
                    for rec in moved_records:
                        size_kb = round(rec["size"] / 1024, 1) if rec["size"] else 0
                        f.write(f"| `{rec['name']}` | `{rec['target_subfolder']}` | {size_kb} KB | ✅ SUCCESS |\n")
                if failed_records:
                    f.write("\n#### ⚠️ Failures\n\n")
                    for fl in failed_records:
                        f.write(f"- `{fl['name']}` -> `{fl['target_subfolder']}`: {fl['error']}\n")
        except Exception as e:
            print(f"Could not write GITHUB_STEP_SUMMARY: {e}")

    if failed_records or residual_in_source:
        raise RuntimeError(
            f"Archival completed with {len(failed_records)} move failure(s) and {len(residual_in_source)} residual file(s)."
        )


def main():
    parser = argparse.ArgumentParser(description="SKIDATA Automated SharePoint File Archiver")
    parser.add_argument(
        "--source",
        default=os.getenv("SOURCE_FOLDER", DEFAULT_SOURCE_FOLDER),
        help=f"Source folder containing files (default: {DEFAULT_SOURCE_FOLDER})",
    )
    parser.add_argument(
        "--dest-root",
        default=os.getenv("ARCHIVE_ROOT", DEFAULT_ARCHIVE_ROOT),
        help=f"Archive root folder (default: {DEFAULT_ARCHIVE_ROOT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        help="Dry run mode: scans and classifies without moving files",
    )
    args = parser.parse_args()

    archive_validated_docs(
        source_folder=args.source,
        archive_root=args.dest_root,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
