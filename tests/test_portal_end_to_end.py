"""
End-to-End Test Suite for SKIDATA Operations Portal, OPA Engine, and Queue Lock.
Tests all endpoints, security policies, concurrency locks, and idempotency guarantees.
"""
import os
import sys
import unittest
from datetime import date, timedelta
from fastapi.testclient import TestClient

# Ensure current directory is on PYTHONPATH
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from opa_engine import OPAPolicyEngine
from queue_lock import QueueLockManager, JobStatus
from portal_server import app, AUTH_USERNAME, AUTH_PASSWORD, AUTH_TOKEN_SECRET


class TestOPAPolicyEngine(unittest.TestCase):
    def setUp(self):
        self.opa = OPAPolicyEngine()

    def test_auth_rule(self):
        # Missing auth token
        allowed, violations = self.opa.evaluate({"operation": "export", "target_folder": "Validated_Docs/old"})
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-101" in v for v in violations))

        # With auth token
        allowed, violations = self.opa.evaluate({"authenticated": True, "operation": "export", "target_folder": "Validated_Docs/old"})
        self.assertTrue(allowed)
        self.assertEqual(len(violations), 0)

    def test_read_only_rule(self):
        # Write / delete operation forbidden
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "operation": "delete_all",
            "target_folder": "Validated_Docs/old"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-201" in v for v in violations))

    def test_production_folder_guardrail(self):
        # Targeting live production folder Skidata_Daily_Files is strictly blocked
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "target_folder": "Skidata_Daily_Files",
            "mode": "daily"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-302" in v for v in violations))

        # Targeting SKIDATA - Odoo is blocked
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "target_folder": "SKIDATA - Odoo",
            "mode": "daily"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-302" in v for v in violations))

        # Safe folder is allowed
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "target_folder": "Validated_Docs/old/Aug",
            "mode": "daily"
        })
        self.assertTrue(allowed)

    def test_date_range_limits(self):
        today = date.today()

        # Valid range within 62 days
        start = (today - timedelta(days=20)).strftime("%Y-%m-%d")
        end = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "mode": "range",
            "start_date": start,
            "end_date": end,
            "target_folder": "Validated_Docs/old"
        })
        self.assertTrue(allowed)

        # Exceeds 62 days limit
        start_far = (today - timedelta(days=100)).strftime("%Y-%m-%d")
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "mode": "range",
            "start_date": start_far,
            "end_date": end,
            "target_folder": "Validated_Docs/old"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-405" in v for v in violations))

        # Future date blocked
        future_end = (today + timedelta(days=5)).strftime("%Y-%m-%d")
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "mode": "range",
            "start_date": start,
            "end_date": future_end,
            "target_folder": "Validated_Docs/old"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-404" in v for v in violations))

        # Start after end blocked
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "mode": "range",
            "start_date": end,
            "end_date": start,
            "target_folder": "Validated_Docs/old"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-403" in v for v in violations))

    def test_single_date_limits(self):
        today = date.today()
        past = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=10)).strftime("%Y-%m-%d")

        # Valid past date
        allowed, _ = self.opa.evaluate({
            "authenticated": True,
            "mode": "single_date",
            "target_date": past,
            "target_folder": "Validated_Docs/old"
        })
        self.assertTrue(allowed)

        # Future single date blocked
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "mode": "single_date",
            "target_date": future,
            "target_folder": "Validated_Docs/old"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-408" in v for v in violations))

    def test_historical_scope_years(self):
        # Valid years (2025, 2026)
        allowed, _ = self.opa.evaluate({
            "authenticated": True,
            "mode": "months",
            "years": [2025, 2026],
            "months": ["August", "September"],
            "target_folder": "Validated_Docs/old"
        })
        self.assertTrue(allowed)

        # Unsupported year (e.g. 2019)
        allowed, violations = self.opa.evaluate({
            "authenticated": True,
            "mode": "months",
            "years": [2019],
            "months": ["August"],
            "target_folder": "Validated_Docs/old"
        })
        self.assertFalse(allowed)
        self.assertTrue(any("OPA-503" in v for v in violations))


class TestQueueLockAndIdempotency(unittest.TestCase):
    def setUp(self):
        self.test_lock_file = os.path.join(BASE_DIR, "tests", "test_lock.json")
        if os.path.exists(self.test_lock_file):
            os.remove(self.test_lock_file)
        self.mgr = QueueLockManager(lock_file=self.test_lock_file)

    def tearDown(self):
        if os.path.exists(self.test_lock_file):
            try:
                os.remove(self.test_lock_file)
            except Exception:
                pass

    def test_single_concurrency_lock(self):
        payload = {"mode": "daily", "target_folder": "Validated_Docs/old"}

        # 1. Acquire first lock
        success, msg, job = self.mgr.try_acquire(payload, requested_by="admin")
        self.assertTrue(success)
        self.assertEqual(job["status"], JobStatus.RUNNING)

        # 2. Second attempt while job is running MUST be rejected
        payload2 = {"mode": "range", "start_date": "2026-10-01", "end_date": "2026-10-10", "target_folder": "Validated_Docs/old"}
        success2, msg2, _ = self.mgr.try_acquire(payload2, requested_by="operator")
        self.assertFalse(success2)
        self.assertIn("Queue Locked", msg2)

        # 3. Complete first job
        self.mgr.complete_job({"records": 100})
        self.assertEqual(self.mgr.current_job["status"], JobStatus.COMPLETED)

        # 4. Now second job can acquire lock
        success3, msg3, job3 = self.mgr.try_acquire(payload2, requested_by="operator")
        self.assertTrue(success3)
        self.assertEqual(job3["status"], JobStatus.RUNNING)

    def test_idempotency_caching(self):
        payload = {
            "mode": "months",
            "years": [2026],
            "months": ["August"],
            "target_folder": "Validated_Docs/old/Aug"
        }

        # First run completes
        success, _, job = self.mgr.try_acquire(payload)
        self.assertTrue(success)
        self.mgr.complete_job({"batches": 1, "status": "all_done"})

        # Immediate exact rerun returns cached idempotent match
        success_dup, msg_dup, job_dup = self.mgr.try_acquire(payload)
        self.assertTrue(success_dup)
        self.assertIn("Idempotent Match", msg_dup)
        self.assertEqual(job_dup["status"], JobStatus.COMPLETED)

    def test_live_diagram_node_progression(self):
        payload = {"mode": "daily", "target_folder": "Validated_Docs/old"}
        self.mgr.try_acquire(payload)

        # Update node to scraper_engine
        self.mgr.update_job(active_node="scraper_engine", progress=25, log_message="Browser started.")
        self.assertEqual(self.mgr.current_job["active_node"], "scraper_engine")
        self.assertEqual(self.mgr.current_job["progress_percent"], 25)
        self.assertIn("skidata_auth", self.mgr.current_job["completed_nodes"])

        # Update table download
        self.mgr.update_job(active_node="table_revenue", table_update=("Revenue in Depth", "downloading"))
        self.assertEqual(self.mgr.current_job["tables_status"]["Revenue in Depth"], "downloading")

        self.mgr.update_job(table_update=("Revenue in Depth", "done"))
        self.assertEqual(self.mgr.current_job["tables_status"]["Revenue in Depth"], "done")


class TestPortalServerEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.auth_headers = {"Authorization": f"Bearer {AUTH_TOKEN_SECRET}"}

    def test_login_success_and_failure(self):
        # Valid credentials
        res = self.client.post("/api/login", json={"username": AUTH_USERNAME, "password": AUTH_PASSWORD})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["token"], AUTH_TOKEN_SECRET)

        # Invalid credentials
        res_fail = self.client.post("/api/login", json={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(res_fail.status_code, 401)

    def test_verify_folder_endpoint(self):
        # Missing auth
        res = self.client.post("/api/verify-folder", json={"folder_input": "Validated_Docs/old"})
        self.assertEqual(res.status_code, 401)

        # Safe staging folder
        res = self.client.post("/api/verify-folder", json={"folder_input": "Validated_Docs/old"}, headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["accessible"])

        # Production folder blocked
        res_prod = self.client.post("/api/verify-folder", json={"folder_input": "Skidata_Daily_Files"}, headers=self.auth_headers)
        self.assertEqual(res_prod.status_code, 200)
        self.assertFalse(res_prod.json()["accessible"])
        self.assertIn("Access Denied", res_prod.json()["error"])

    def test_execute_blocked_by_opa(self):
        # Request with > 62 days range blocked by OPA with HTTP 403
        payload = {
            "mode": "range",
            "start_date": "2026-01-01",
            "end_date": "2026-06-01",
            "target_folder": "Validated_Docs/old"
        }
        res = self.client.post("/api/execute", json=payload, headers=self.auth_headers)
        self.assertEqual(res.status_code, 403)
        self.assertIn("OPA Policy Violation", res.json()["detail"]["error"])

    def test_get_job_status(self):
        res = self.client.get("/api/job/status", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        self.assertIn("status", res.json())

    def test_serve_portal_html(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("SKIDATA Control Plane", res.text)
        self.assertIn("Download Current / Latest Data", res.text)
        self.assertIn("Historic Range Fill", res.text)


if __name__ == "__main__":
    unittest.main()
