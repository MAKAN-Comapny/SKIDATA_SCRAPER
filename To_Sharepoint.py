"""
Upload cleaned SKIDATA export files from Cleaned-Exports/ to Microsoft SharePoint.
Run Transformer.py first to ensure Cleaned-Exports/ is populated.
"""
import os
import re
import glob
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SITE_NAME = os.getenv("SHAREPOINT_SITE_NAME")
TARGET_FOLDER_PATH = os.getenv("TARGET_FOLDER_PATH") or "SKIDATA-Cleaned-Exports"

CLEANED_EXPORTS_DIR = "Cleaned-Exports"


def get_access_token():
    """Get Microsoft Graph access token using app credentials."""
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    r = requests.post(token_url, data=data)
    r.raise_for_status()
    return r.json()["access_token"]


def get_site_id(headers):
    """Find SharePoint site ID by URL or display name."""
    site_input = SITE_NAME.strip()

    # Full URL: https://tenant.sharepoint.com/sites/SiteName
    url_match = re.match(r"https?://([^/]+)(/.*)?", site_input)
    if url_match:
        hostname = url_match.group(1)
        path = (url_match.group(2) or "/").rstrip("/") or "/"
        if not path.startswith("/"):
            path = "/" + path
        get_url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:{path}"
        r = requests.get(get_url, headers=headers)
        r.raise_for_status()
        return r.json()["id"]

    # Site name only: use search
    site_search_url = f"https://graph.microsoft.com/v1.0/sites?search={requests.utils.quote(site_input)}"
    r = requests.get(site_search_url, headers=headers)
    r.raise_for_status()
    sites = r.json().get("value", [])
    if not sites:
        raise ValueError(f"No site found with name or URL: {SITE_NAME}")
    return sites[0]["id"]


def get_target_folder(folder=None):
    return folder or os.getenv("TARGET_FOLDER_PATH") or TARGET_FOLDER_PATH


def ensure_folder_path(site_id, headers, folder=None):
    """Create nested folders if they don't exist."""
    target_folder = get_target_folder(folder)
    parts = [p for p in target_folder.split("/") if p.strip()]
    current = ""

    for part in parts:
        current = f"{current}/{part}" if current else part
        check = requests.get(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{current}",
            headers=headers,
        )

        if check.status_code == 404:
            parent = current.rsplit("/", 1)[0] if "/" in current else ""
            create_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{parent}:/children"
                if parent
                else f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children"
            )
            folder_payload = {
                "name": part,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "replace",
            }
            requests.post(create_url, headers=headers, json=folder_payload)


def upload_large_file(site_id, headers, file_path, folder=None):
    """Upload large file (> 4MB) to SharePoint using chunked upload session."""
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    target_dir = get_target_folder(folder)

    session_url = (
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:"
        f"/{target_dir}/{file_name}:/createUploadSession"
    )

    payload = {
        "item": {
            "@microsoft.graph.conflictBehavior": "replace",
            "name": file_name,
        }
    }

    r = requests.post(session_url, headers=headers, json=payload)
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]

    # 5MB chunks (must be multiple of 320 KiB)
    chunk_size = 5 * 1024 * 1024
    last_response = None
    with open(file_path, "rb") as f:
        start = 0
        while start < file_size:
            chunk = f.read(chunk_size)
            end = start + len(chunk) - 1
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
            }
            res = requests.put(upload_url, headers=chunk_headers, data=chunk)
            res.raise_for_status()
            last_response = res
            start = end + 1

    return last_response.json() if last_response else {}


def upload_file(site_id, headers, file_path, folder=None):
    """Upload a single file to SharePoint (uses chunked session if > 4MB)."""
    file_size = os.path.getsize(file_path)
    if file_size > 4 * 1024 * 1024:
        return upload_large_file(site_id, headers, file_path, folder)

    file_name = os.path.basename(file_path)
    target_dir = get_target_folder(folder)
    upload_url = (
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:"
        f"/{target_dir}/{file_name}:/content"
    )

    with open(file_path, "rb") as f:
        r = requests.put(upload_url, headers=headers, data=f)

    r.raise_for_status()
    return r.json()


def upload_files(files, folder):
    """Upload the given files to another SharePoint folder, deleting each local copy."""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    site_id = get_site_id(headers)
    ensure_folder_path(site_id, headers, folder)
    for fp in files:
        result = upload_file(site_id, headers, fp, folder)
        print(f"  OK: {os.path.basename(fp)} -> {result.get('webUrl', 'uploaded')}")
        os.remove(fp)


def main():
    missing = [k for k, v in {
        "TENANT_ID": TENANT_ID,
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": CLIENT_SECRET,
        "SHAREPOINT_SITE_NAME": SITE_NAME,
    }.items() if not v or not str(v).strip()]
    if missing:
        raise SystemExit(
            f"Missing required .env variables: {', '.join(missing)}. "
            "Fill them in .env and try again."
        )

    if not os.path.isdir(CLEANED_EXPORTS_DIR):
        raise SystemExit(
            f"Folder '{CLEANED_EXPORTS_DIR}' not found. Run Transformer.py first."
        )

    files = glob.glob(os.path.join(CLEANED_EXPORTS_DIR, "*.xlsx"))
    if not files:
        raise SystemExit(
            f"No .xlsx files in '{CLEANED_EXPORTS_DIR}'. Run Transformer.py first."
        )

    print("Getting access token...")
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    print("Resolving SharePoint site...")
    site_id = get_site_id(headers)

    print("Ensuring folder path exists...")
    ensure_folder_path(site_id, headers)

    print(f"Uploading {len(files)} file(s)...")
    for fp in files:
        try:
            result = upload_file(site_id, headers, fp)
            print(f"  OK: {os.path.basename(fp)} -> {result.get('webUrl', 'uploaded')}")
            os.remove(fp)
        except requests.RequestException as e:
            print(f"  FAIL: {os.path.basename(fp)} - {e}")
            raise

    print("Done.")


if __name__ == "__main__":
    main()
