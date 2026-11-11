"""
Queue Lock & Idempotency Manager for SKIDATA Data Extraction.
Guarantees single-concurrency execution and deduplicates identical runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


class JobStatus:
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class QueueLockManager:
    """
    Thread-safe concurrency lock and idempotency engine.
    """

    def __init__(self, lock_file: str = "portal_lock.json"):
        self._lock = threading.Lock()
        self.lock_file = lock_file
        self.current_job: Optional[Dict[str, Any]] = None
        self.job_history: List[Dict[str, Any]] = []
        self._job_listeners: List[Any] = []
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # If server was restarted while job was marked running, reset to failed
                    if data.get("status") == JobStatus.RUNNING:
                        data["status"] = JobStatus.FAILED
                        data["error"] = "Process terminated due to server restart."
                    self.current_job = data
            except Exception:
                self.current_job = None

    def _save_state(self):
        try:
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(self.current_job, f, indent=2)
        except Exception as e:
            print(f"[QueueLock] Failed to persist state: {e}")

    def compute_idempotency_key(self, payload: Dict[str, Any]) -> str:
        """Generate SHA-256 idempotency fingerprint based on execution parameters."""
        relevant = {
            "mode": payload.get("mode"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "target_date": payload.get("target_date"),
            "years": sorted(list(map(int, payload.get("years", [])))),
            "months": sorted(payload.get("months", [])),
            "target_folder": payload.get("target_folder", "").strip().lower(),
            "validate": bool(payload.get("validate", True)),
        }
        serialized = json.dumps(relevant, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def try_acquire(self, payload: Dict[str, Any], requested_by: str = "admin") -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Attempt to acquire execution lock.
        Returns: (success: bool, message: str, job_info: dict)
        """
        with self._lock:
            # 1. Concurrency Check: Is a job currently actively running?
            if self.current_job and self.current_job.get("status") == JobStatus.RUNNING:
                running_id = self.current_job.get("id")
                elapsed = int(time.time() - self.current_job.get("start_time", time.time()))
                return (
                    False,
                    f"Queue Locked: Job #{running_id} is currently running ({elapsed}s elapsed). Please wait for it to complete.",
                    self.current_job,
                )

            # 2. Idempotency Check: Did this exact job run recently with success?
            idempotency_key = self.compute_idempotency_key(payload)
            recent_threshold = 900  # 15 minutes window
            now = time.time()

            if (
                self.current_job
                and self.current_job.get("idempotency_key") == idempotency_key
                and self.current_job.get("status") == JobStatus.COMPLETED
                and (now - self.current_job.get("end_time", 0)) < recent_threshold
            ):
                return (
                    True,
                    f"Idempotent Match: Identical extraction was completed {int(now - self.current_job.get('end_time', 0))}s ago.",
                    self.current_job,
                )

            # 3. Create New Job Record
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.current_job = {
                "id": job_id,
                "idempotency_key": idempotency_key,
                "status": JobStatus.RUNNING,
                "start_time": now,
                "start_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": None,
                "requested_by": requested_by,
                "payload": payload,
                "current_node": "init",  # Used for interactive diagram animation
                "completed_nodes": [],
                "active_node": "skidata_auth",
                "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id} queued and lock acquired."],
                "progress_percent": 5,
                "tables_status": {
                    "Revenue in Depth": "pending",
                    "Access in Depth": "pending",
                    "System Event in Depth": "pending",
                    "Parking Transactions": "pending",
                },
                "table_details": None,
                "validation_enabled": bool(payload.get("validate", True)),
                "validation_report": None,
                "result": None,
                "error": None,
            }
            self._save_state()
            return True, f"Lock acquired for Job #{job_id}", self.current_job

    def update_job(
        self,
        active_node: Optional[str] = None,
        log_message: Optional[str] = None,
        progress: Optional[int] = None,
        table_update: Optional[Tuple[str, str]] = None,
        validation_report: Optional[Dict[str, Any]] = None,
    ):
        """Update live execution progress for frontend diagram."""
        with self._lock:
            if not self.current_job:
                return

            if active_node and active_node != self.current_job.get("active_node"):
                if self.current_job.get("active_node"):
                    self.current_job["completed_nodes"].append(self.current_job["active_node"])
                self.current_job["active_node"] = active_node

            if log_message:
                ts = datetime.now().strftime("%H:%M:%S")
                self.current_job["logs"].append(f"[{ts}] {log_message}")

            if progress is not None:
                self.current_job["progress_percent"] = max(0, min(100, progress))

            if table_update:
                tbl_name, tbl_state = table_update
                if tbl_name in self.current_job["tables_status"]:
                    self.current_job["tables_status"][tbl_name] = tbl_state

            if validation_report is not None:
                self.current_job["validation_report"] = validation_report
                self.current_job["validation_enabled"] = validation_report.get("validation_enabled", True)
                if validation_report.get("tables"):
                    self.current_job["table_details"] = validation_report["tables"]
                    for k, info in validation_report["tables"].items():
                        if k in self.current_job["tables_status"] and info.get("status"):
                            self.current_job["tables_status"][k] = info["status"]

            self._save_state()

    def complete_job(self, result_summary: Dict[str, Any], validation_report: Optional[Dict[str, Any]] = None):
        with self._lock:
            if not self.current_job:
                return
            now = time.time()
            self.current_job["status"] = JobStatus.COMPLETED
            self.current_job["end_time"] = now
            self.current_job["progress_percent"] = 100
            self.current_job["result"] = result_summary
            if validation_report is not None:
                self.current_job["validation_report"] = validation_report
                self.current_job["validation_enabled"] = validation_report.get("validation_enabled", True)
                if validation_report.get("tables"):
                    self.current_job["table_details"] = validation_report["tables"]
            if self.current_job.get("active_node"):
                self.current_job["completed_nodes"].append(self.current_job["active_node"])
            self.current_job["active_node"] = "destination_upload"
            ts = datetime.now().strftime("%H:%M:%S")
            self.current_job["logs"].append(f"[{ts}] Job #{self.current_job['id']} completed successfully.")
            self._save_state()

    def fail_job(self, error_message: str, validation_report: Optional[Dict[str, Any]] = None):
        with self._lock:
            if not self.current_job:
                return
            now = time.time()
            self.current_job["status"] = JobStatus.FAILED
            self.current_job["end_time"] = now
            self.current_job["error"] = error_message
            if validation_report is not None:
                self.current_job["validation_report"] = validation_report
                self.current_job["validation_enabled"] = validation_report.get("validation_enabled", True)
                if validation_report.get("tables"):
                    self.current_job["table_details"] = validation_report["tables"]
                    for k, info in validation_report["tables"].items():
                        if k in self.current_job["tables_status"] and info.get("status"):
                            self.current_job["tables_status"][k] = info["status"]
            ts = datetime.now().strftime("%H:%M:%S")
            self.current_job["logs"].append(f"[{ts}] ERROR: {error_message}")
            self._save_state()

    def release_lock(self):
        """Emergency release."""
        with self._lock:
            if self.current_job and self.current_job.get("status") == JobStatus.RUNNING:
                self.current_job["status"] = JobStatus.FAILED
                self.current_job["error"] = "Lock manually released."
                self._save_state()

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.current_job) if self.current_job else {"status": JobStatus.IDLE}
