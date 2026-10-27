"""
To_Sharepoint Backfill
----------------------
Uploads historical backfill files from 'Cleaned-Exports-Backfill/' (or specified directory)
to Microsoft SharePoint under the isolated folder (e.g. 'Validated_Docs/old/Aug' or 'Validated_Docs/old/Sep').
Never uploads to 'Skidata_Daily_Files' to ensure production database safety.
Supports large files (>4MB) via Microsoft Graph createUploadSession.
"""
import glob
import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SITE_NAME = os.getenv("SHAREPOINT_SITE_NAME")

DEFAULT_TARGET_FOLDER = os.getenv("TARGET_FOLDER_PATH_BACKFILL") or "Validated_Docs"
CLEANED_EXPORTS_DIR = "Cleaned-Exports-Backfill"


def encode_drive_path(path: str) -> str:
    """URL-encode each folder/file segment for Graph drive paths."""
    return "/".join(requests.utils.quote(part, safe="") for part in path.split("/") if part)


def get_access_token():
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
    site_input = SITE_NAME.strip()
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

    site_search_url = f"https://graph.microsoft.com/v1.0/sites?search={requests.utils.quote(site_input)}"
    r = requests.get(site_search_url, headers=headers)
    r.raise_for_status()
    sites = r.json().get("value", [])
    if not sites:
        raise ValueError(f"No site found with name or URL: {SITE_NAME}")
    return sites[0]["id"]


def ensure_folder_path(site_id, headers, target_folder: str = DEFAULT_TARGET_FOLDER):
    parts = [p for p in target_folder.split("/") if p.strip()]
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        encoded = encode_drive_path(current)
        check = requests.get(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{encoded}",
            headers=headers,
        )
        if check.status_code == 404:
            parent = current.rsplit("/", 1)[0] if "/" in current else ""
            encoded_parent = encode_drive_path(parent) if parent else ""
            create_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{encoded_parent}:/children"
                if parent
                else f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children"
            )
            folder_payload = {
                "name": part,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "replace",
            }
            requests.post(create_url, headers=headers, json=folder_payload)


def upload_file(site_id, headers, file_path: str, target_folder: str = DEFAULT_TARGET_FOLDER):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    encoded_folder = encode_drive_path(target_folder)
    encoded_name = requests.utils.quote(file_name, safe="")

    # For files > 4MB, use Graph upload session to avoid 413 error
    if file_size > 4 * 1024 * 1024:
        session_url = (
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:"
            f"/{encoded_folder}/{encoded_name}:/createUploadSession"
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

        chunk_size = 10 * 1024 * 1024  # 10MB chunk
        with open(file_path, "rb") as f:
            start = 0
            while start < file_size:
                chunk = f.read(chunk_size)
                end = start + len(chunk) - 1
                chunk_headers = {
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(len(chunk)),
                }
                res = requests.put(upload_url, headers=chunk_headers, data=chunk)
                res.raise_for_status()
                start += len(chunk)
        return res.json()
    else:
        upload_url = (
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:"
            f"/{encoded_folder}/{encoded_name}:/content"
        )
        with open(file_path, "rb") as f:
            r = requests.put(upload_url, headers=headers, data=f)
        r.raise_for_status()
        return r.json()


def main(target_folder: str | None = None, source_dir: str | None = None):
    target = target_folder or os.getenv("TARGET_FOLDER_PATH_BACKFILL") or DEFAULT_TARGET_FOLDER
    source = source_dir or CLEANED_EXPORTS_DIR

    missing = [k for k, v in {
        "TENANT_ID": TENANT_ID,
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": CLIENT_SECRET,
        "SHAREPOINT_SITE_NAME": SITE_NAME,
    }.items() if not v or not str(v).strip()]
    if missing:
        raise SystemExit(
            f"Missing required .env variables: {', '.join(missing)}.\n"
            "Please provide them in your local .env file."
        )

    if not os.path.isdir(source):
        print(f"Notice: Folder '{source}' not found. Nothing to upload.")
        return

    files = sorted(glob.glob(os.path.join(source, "*.xlsx")))
    if not files:
        print(f"Notice: No .xlsx files found in '{source}' to upload.")
        return

    print(f"Uploading backfill files to SharePoint folder: '{target}'...")
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    site_id = get_site_id(headers)
    ensure_folder_path(site_id, headers, target)

    print(f"Uploading {len(files)} file(s)...")
    import time
    for fp in files:
        for attempt in range(3):
            try:
                result = upload_file(site_id, headers, fp, target)
                print(f"  Uploaded: {os.path.basename(fp)} ({os.path.getsize(fp):,} bytes) -> {target}/")
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  Upload attempt {attempt+1} failed for {os.path.basename(fp)} ({e}), retrying in 3s...")
                time.sleep(3)

    print(f"\nAll files successfully uploaded to '{target}' on SharePoint!")


if __name__ == "__main__":
    main()
