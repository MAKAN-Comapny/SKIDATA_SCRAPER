"""
FastAPI Server for SKIDATA Ingestion & Historical Fill Portal.
Provides endpoints for authentication, folder validation, OPA evaluation, queue management, and live status.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from opa_engine import OPAPolicyEngine
from queue_lock import QueueLockManager, JobStatus
from job_runner import JobRunner

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTAL_DIR = os.path.join(BASE_DIR, "portal")
os.makedirs(PORTAL_DIR, exist_ok=True)

app = FastAPI(
    title="SKIDATA Data Operations Portal",
    description="Enterprise Ingestion & Historical Backfill Control Plane",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single Universal Credentials
AUTH_USERNAME = os.getenv("PORTAL_USER", "admin")
AUTH_PASSWORD = os.getenv("PORTAL_PASSWORD", "Makan@2026!")
AUTH_TOKEN_SECRET = "skidata_makan_bearer_token_2026"

# Core Managers
opa_engine = OPAPolicyEngine()
queue_mgr = QueueLockManager(lock_file=os.path.join(BASE_DIR, "portal_lock.json"))
job_runner = JobRunner(queue_mgr=queue_mgr, base_dir=BASE_DIR)


# --- Models ---
class LoginRequest(BaseModel):
    username: str
    password: str


class FolderVerifyRequest(BaseModel):
    folder_input: str


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str  # 'daily', 'months', 'range', 'single_date'
    target_folder: str = "Validated_Docs/old"
    years: Optional[List[int]] = None
    months: Optional[List[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    target_date: Optional[str] = None
    merge_option: Optional[str] = "separate"
    validate_data: bool = Field(default=True, alias="validate")


# --- Authentication Dependency ---
def verify_auth_token(authorization: Optional[str] = Header(None)) -> bool:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token != AUTH_TOKEN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid session or token expired")
    return True


# --- API Routes ---
@app.post("/api/login")
def login(req: LoginRequest):
    if req.username == AUTH_USERNAME and req.password == AUTH_PASSWORD:
        return {
            "success": True,
            "token": AUTH_TOKEN_SECRET,
            "user": req.username,
            "role": "Makan Operations Administrator",
        }
    raise HTTPException(status_code=401, detail="Invalid username or password.")


def _parse_folder_input(raw: str) -> str:
    """Parse user input: extracts SharePoint drive path whether full URL or simple name."""
    s = raw.strip()
    if not s:
        return "Validated_Docs/old"

    if "sharepoint.com" in s:
        # e.g. https://makanpak.sharepoint.com/sites/SKIDATAFILES/Shared%20Documents/Validated_Docs/old/Aug
        match = re.search(r"Shared%20Documents/([^?#]+)", s)
        if match:
            return urllib.parse.unquote(match.group(1)).strip("/")
        match2 = re.search(r"sites/[^/]+/([^?#]+)", s)
        if match2:
            return urllib.parse.unquote(match2.group(1)).strip("/")

    return s.strip("/")


@app.post("/api/verify-folder")
def verify_folder(req: FolderVerifyRequest, authenticated: bool = Depends(verify_auth_token)):
    parsed_path = _parse_folder_input(req.folder_input)
    norm = parsed_path.lower()

    # 1. Guardrail Check: Block live production folder
    if any(forbidden in norm for forbidden in ["skidata_daily_files", "skidata - odoo"]):
        return {
            "accessible": False,
            "path": parsed_path,
            "error": "Access Denied: Writing directly to the live production daily folder is locked.",
        }

    # 2. Check Graph API connectivity if credentials are present
    tenant_id = os.getenv("TENANT_ID", "").strip()
    client_id = os.getenv("CLIENT_ID", "").strip()
    client_secret = os.getenv("CLIENT_SECRET", "").strip()
    site_name = os.getenv("SHAREPOINT_SITE_NAME", "").strip()

    if tenant_id and client_id and client_secret:
        try:
            # Live SharePoint check via Graph API
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            r = requests.post(token_url, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            }, timeout=10)
            r.raise_for_status()
            access_token = r.json()["access_token"]
            headers = {"Authorization": f"Bearer {access_token}"}

            # Resolve site ID
            site_r = requests.get(f"https://graph.microsoft.com/v1.0/sites?search={urllib.parse.quote('SKIDATAFILES')}", headers=headers, timeout=10)
            site_id = site_r.json()["value"][0]["id"]

            # Check drive path
            encoded_drive = "/".join(urllib.parse.quote(p, safe="") for p in parsed_path.split("/") if p)
            chk = requests.get(f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:/{encoded_drive}", headers=headers, timeout=10)

            if chk.status_code == 200:
                return {"accessible": True, "path": parsed_path, "message": "SharePoint folder exists and is fully accessible."}
            elif chk.status_code == 404:
                # Can be auto-created safely by backfill script if in Validated_Docs
                if parsed_path.startswith("Validated_Docs") or parsed_path.startswith("Ski Data"):
                    return {"accessible": True, "path": parsed_path, "message": "Folder will be automatically provisioned on upload."}
                return {"accessible": False, "path": parsed_path, "error": f"Folder '{parsed_path}' not found in SharePoint."}

        except Exception as exc:
            print(f"[FolderCheck] Live Graph check warning: {exc}")

    # Fallback / Local test validation: Validates path formatting
    if parsed_path and len(parsed_path) >= 3 and not re.search(r'[<>:"|?*]', parsed_path):
        return {
            "accessible": True,
            "path": parsed_path,
            "message": f"Target path verified: '{parsed_path}'",
        }
    else:
        return {
            "accessible": False,
            "path": parsed_path,
            "error": "Invalid folder path format.",
        }


@app.post("/api/execute")
def execute_extraction(req: ExecutionRequest, authenticated: bool = Depends(verify_auth_token)):
    payload = req.model_dump(by_alias=True)
    payload["authenticated"] = True
    payload["target_folder"] = _parse_folder_input(req.target_folder)
    payload["operation"] = "export"

    # --- Step 1: Open Policy Agent (OPA) Evaluation ---
    allowed, violations = opa_engine.evaluate(payload)
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "OPA Policy Violation: Request blocked by security rules.",
                "violations": violations,
            },
        )

    # --- Step 2: Concurrency Lock & Idempotency Check ---
    started, msg, job_info = job_runner.start_job(payload, requested_by="admin")
    if not started:
        raise HTTPException(status_code=409, detail=msg)

    return {
        "success": True,
        "message": msg,
        "job": job_info,
    }


@app.get("/api/job/status")
def get_job_status(authenticated: bool = Depends(verify_auth_token)):
    return queue_mgr.get_state()


@app.get("/api/status")
def get_status_compat():
    return queue_mgr.get_state()


@app.post("/api/job/cancel")
def cancel_job(authenticated: bool = Depends(verify_auth_token)):
    queue_mgr.release_lock()
    return {"success": True, "message": "Queue lock released."}


# --- Serve Static Portal ---
app.mount("/static", StaticFiles(directory=PORTAL_DIR), name="static")


@app.get("/")
def serve_index():
    index_file = os.path.join(PORTAL_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return JSONResponse({"status": "Portal UI under construction. Use API endpoints."})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("portal_server:app", host="127.0.0.1", port=8000, reload=True)
