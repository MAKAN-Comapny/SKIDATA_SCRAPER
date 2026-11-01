"""
Job Runner Engine for SKIDATA Data Extraction.
Orchestrates daily rolling and historical backfills asynchronously in the background.
Tracks progress, live steps, and component states for the visual architecture diagram.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from queue_lock import QueueLockManager, JobStatus


class JobRunner:
    def __init__(self, queue_mgr: QueueLockManager, base_dir: str):
        self.queue_mgr = queue_mgr
        self.base_dir = base_dir
        self.python_exe = os.path.join(base_dir, ".venv", "Scripts", "python.exe")
        if not os.path.exists(self.python_exe):
            self.python_exe = sys.executable

    def start_job(self, payload: Dict[str, Any], requested_by: str = "admin") -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Acquires queue lock and launches background worker thread.
        """
        acquired, msg, job_info = self.queue_mgr.try_acquire(payload, requested_by)
        if not acquired:
            return False, msg, job_info

        # If it was an idempotent match (already completed recently), don't start duplicate thread
        if job_info and job_info.get("status") == JobStatus.COMPLETED:
            return True, msg, job_info

        # Launch background thread
        thread = threading.Thread(
            target=self._run_job_thread,
            args=(payload,),
            daemon=True,
            name=f"JobRunner-{job_info['id']}",
        )
        thread.start()
        return True, msg, job_info

    def _run_job_thread(self, payload: Dict[str, Any]):
        mode = payload.get("mode", "daily")
        target_folder = payload.get("target_folder", "Validated_Docs/old").strip()

        try:
            self.queue_mgr.update_job(
                active_node="skidata_auth",
                log_message=f"Starting job in '{mode}' mode. Target folder: '{target_folder}'",
                progress=10,
            )

            if mode == "daily":
                self._execute_daily(target_folder)
            elif mode == "months":
                self._execute_months(payload, target_folder)
            elif mode == "range":
                self._execute_range(payload, target_folder)
            elif mode == "single_date":
                self._execute_single_date(payload, target_folder)
            else:
                raise ValueError(f"Unknown execution mode: {mode}")

        except Exception as e:
            self.queue_mgr.fail_job(str(e))

    def _execute_daily(self, target_folder: str):
        """Execute daily rolling 5-day scraper."""
        cmd = [self.python_exe, "main.py"]
        env = os.environ.copy()
        env["TARGET_FOLDER_PATH"] = target_folder
        env["HEADLESS"] = "true"
        env["SKIP_ODOO_EXPORT"] = "true"

        self.queue_mgr.update_job(
            active_node="scraper_engine",
            log_message="Launching daily scraper subprocess...",
            progress=20,
        )

        success, summary = self._run_subprocess_stream(cmd, env, context="Daily Scraper")
        if success:
            self.queue_mgr.complete_job({
                "mode": "daily",
                "folder": target_folder,
                "summary": summary,
                "tables_fetched": ["Revenue in Depth", "Access in Depth", "System Event in Depth", "Parking Transactions"],
            })
        else:
            raise RuntimeError(summary.get("error", "Daily scraper run failed."))

    def _execute_months(self, payload: Dict[str, Any], target_folder: str):
        """Execute multi-year and multi-month historical backfill."""
        years = sorted(list(map(int, payload.get("years", [2026]))))
        months = payload.get("months", [])
        total_tasks = len(years) * len(months)
        task_idx = 0
        all_results = []

        self.queue_mgr.update_job(
            active_node="scraper_engine",
            log_message=f"Starting historical backfill for {len(years)} year(s) and {len(months)} month(s) ({total_tasks} total batches)...",
            progress=15,
        )

        for year in years:
            for month in months:
                task_idx += 1
                batch_name = f"{month} {year}"
                sub_folder = f"{target_folder.rstrip('/')}/{month[:3]}" if not target_folder.endswith(month[:3]) else target_folder

                self.queue_mgr.update_job(
                    active_node="scraper_engine",
                    log_message=f"[{task_idx}/{total_tasks}] Processing batch: {batch_name} -> {sub_folder}...",
                    progress=int(15 + (task_idx / total_tasks) * 75),
                )

                cmd = [
                    self.python_exe,
                    "main_backfill.py",
                    "--month", f"{month} {year}",
                    "--folder", sub_folder,
                    "--headless",
                ]
                env = os.environ.copy()
                env["TARGET_FOLDER_PATH_BACKFILL"] = sub_folder

                success, summary = self._run_subprocess_stream(cmd, env, context=batch_name)
                all_results.append({
                    "batch": batch_name,
                    "folder": sub_folder,
                    "success": success,
                    "summary": summary,
                })

                if not success:
                    # Report partial month error (e.g. month data not in portal)
                    err_msg = summary.get("error", "Unknown error")
                    self.queue_mgr.update_job(
                        log_message=f"Notice: Batch {batch_name} did not complete: {err_msg}",
                    )

        self.queue_mgr.complete_job({
            "mode": "months",
            "batches_processed": task_idx,
            "results": all_results,
            "target_folder": target_folder,
        })

    def _execute_range(self, payload: Dict[str, Any], target_folder: str):
        """Execute custom date range backfill."""
        start_d = payload.get("start_date")
        end_d = payload.get("end_date")
        range_spec = f"{start_d} to {end_d}"

        self.queue_mgr.update_job(
            active_node="scraper_engine",
            log_message=f"Starting custom range backfill: {range_spec} -> {target_folder}...",
            progress=20,
        )

        cmd = [
            self.python_exe,
            "main_backfill.py",
            "--range", range_spec,
            "--folder", target_folder,
            "--headless",
        ]
        env = os.environ.copy()
        env["TARGET_FOLDER_PATH_BACKFILL"] = target_folder

        success, summary = self._run_subprocess_stream(cmd, env, context=f"Range {range_spec}")
        if success:
            self.queue_mgr.complete_job({
                "mode": "range",
                "range": range_spec,
                "folder": target_folder,
                "summary": summary,
            })
        else:
            raise RuntimeError(summary.get("error", "Range backfill failed."))

    def _execute_single_date(self, payload: Dict[str, Any], target_folder: str):
        """Execute single historic date backfill."""
        target_d = payload.get("target_date")
        range_spec = f"{target_d} to {target_d}"

        self.queue_mgr.update_job(
            active_node="scraper_engine",
            log_message=f"Starting single date extraction: {target_d} -> {target_folder}...",
            progress=20,
        )

        cmd = [
            self.python_exe,
            "main_backfill.py",
            "--range", range_spec,
            "--folder", target_folder,
            "--headless",
        ]
        env = os.environ.copy()
        env["TARGET_FOLDER_PATH_BACKFILL"] = target_folder

        success, summary = self._run_subprocess_stream(cmd, env, context=f"Date {target_d}")
        if success:
            self.queue_mgr.complete_job({
                "mode": "single_date",
                "date": target_d,
                "folder": target_folder,
                "summary": summary,
            })
        else:
            raise RuntimeError(summary.get("error", f"Single date extraction for {target_d} failed."))

    def _run_subprocess_stream(self, cmd: List[str], env: Dict[str, str], context: str) -> Tuple[bool, Dict[str, Any]]:
        """Run subprocess, stream stdout to queue logs, and track node updates."""
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.base_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            last_error = None
            for raw_line in iter(proc.stdout.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue

                # Stream log to QueueLock
                self.queue_mgr.update_job(log_message=line)

                # Parse table state updates for live visual diagram
                if "Revenue in Depth" in line:
                    if "Opened" in line or "detected" in line:
                        self.queue_mgr.update_job(active_node="table_revenue", table_update=("Revenue in Depth", "downloading"))
                    elif "completed" in line or "Saved" in line:
                        self.queue_mgr.update_job(table_update=("Revenue in Depth", "done"))

                elif "Access in Depth" in line:
                    if "Opened" in line or "detected" in line:
                        self.queue_mgr.update_job(active_node="table_access", table_update=("Access in Depth", "downloading"))
                    elif "completed" in line or "Saved" in line:
                        self.queue_mgr.update_job(table_update=("Access in Depth", "done"))

                elif "System Event in Depth" in line:
                    if "Opened" in line or "detected" in line:
                        self.queue_mgr.update_job(active_node="table_system_event", table_update=("System Event in Depth", "downloading"))
                    elif "completed" in line or "Saved" in line:
                        self.queue_mgr.update_job(table_update=("System Event in Depth", "done"))

                elif "Parking Transactions" in line:
                    if "Opened" in line or "Fetching" in line:
                        self.queue_mgr.update_job(active_node="table_parking", table_update=("Parking Transactions", "downloading"))
                    elif "SUCCESS: Saved" in line or "completed" in line:
                        self.queue_mgr.update_job(table_update=("Parking Transactions", "done"))

                elif "Uploading to SharePoint" in line or "Step 4/4" in line:
                    self.queue_mgr.update_job(active_node="destination_upload", progress=90)

                # Track error lines
                if "error" in line.lower() or "traceback" in line.lower() or "exception" in line.lower():
                    last_error = line

            proc.stdout.close()
            return_code = proc.wait()

            if return_code == 0:
                return True, {"status": "success", "context": context}
            else:
                return False, {"status": "failed", "error": last_error or f"Process exited with code {return_code}"}

        except Exception as exc:
            return False, {"status": "failed", "error": str(exc)}
