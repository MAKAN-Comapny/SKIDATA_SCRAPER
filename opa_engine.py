"""
Open Policy Agent (OPA) Rules Engine for SKIDATA Data Extraction.
Implements Policy-as-Code for authorization, safety guardrails, date ranges, and destination folder protection.
"""
from __future__ import annotations

import re
from datetime import datetime, date
from typing import Any, Dict, List, Tuple


class OPAPolicyEngine:
    """
    Evaluates extraction requests against enterprise governance and safety policies.
    """

    FORBIDDEN_FOLDERS = {
        "skidata_daily_files",
        "skidata-daily-files",
        "skidata - odoo",
        "skidata_odoo",
        "ski-data-daily",
    }

    ALLOWED_ROOT_FOLDERS = {
        "validated_docs",
        "ski data 5",
        "odoo_testing_sumit",
        "validation_docs",
        "odoo_aug_sep",
    }

    MAX_HISTORICAL_DAYS = 62  # 2 months limit per batch job to prevent server memory saturation

    def __init__(self):
        self.rules = [
            self._rule_auth,
            self._rule_read_only_safety,
            self._rule_folder_guardrail,
            self._rule_date_range_limits,
            self._rule_historical_scope,
        ]

    def _normalize_folder(self, folder_str: str) -> str:
        """Strip SharePoint URL prefix if present and normalize."""
        clean = folder_str.strip()
        # Handle full SharePoint URL
        if "sharepoint.com" in clean:
            match = re.search(r"Shared%20Documents/(.+?)($|[?#])", clean)
            if match:
                clean = requests_unquote(match.group(1))
            else:
                parts = clean.split("/")
                clean = "/".join(parts[-3:])
        return clean.strip("/\\ ")

    def evaluate(self, request_payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Evaluate request payload. Returns (allowed: bool, reasons: list of strings).
        """
        violations: List[str] = []

        for rule in self.rules:
            passed, reason = rule(request_payload)
            if not passed:
                violations.append(reason)

        return (len(violations) == 0, violations)

    def _rule_auth(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        """OPA Rule: Request must contain a valid authenticated session."""
        auth_token = req.get("auth_token") or req.get("authenticated")
        if not auth_token:
            return False, "OPA-101: Unauthorized request. Authentication token is missing or expired."
        return True, ""

    def _rule_read_only_safety(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        """OPA Rule: Enforce 100% read-only operations against SkiData portal."""
        operation = req.get("operation", "export").lower()
        if operation not in ("export", "scrape", "read", "fetch"):
            return False, f"OPA-201: Forbidden operation '{operation}'. Only strictly READ-ONLY data export is permitted."
        return True, ""

    def _rule_folder_guardrail(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        """OPA Rule: Block writes to production directories (e.g. Skidata_Daily_Files)."""
        folder = req.get("target_folder", "").strip()
        if not folder:
            return False, "OPA-301: Target destination folder must be specified."

        norm = self._normalize_folder(folder).lower()
        root_seg = norm.split("/")[0].strip()

        if root_seg in self.FORBIDDEN_FOLDERS:
            return False, (
                f"OPA-302: Safety Guardrail Triggered - Uploading to production folder '{folder}' is forbidden. "
                "Only staging or old archive folders (e.g. Validated_Docs/old) are permitted."
            )

        if root_seg not in self.ALLOWED_ROOT_FOLDERS:
            return False, (
                f"OPA-303: Folder '{folder}' does not exist in SharePoint document library. "
                "Permitted destination folders must be inside 'Validated_Docs' or 'Ski Data 5'."
            )

        return True, ""

    def _rule_date_range_limits(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        """OPA Rule: Range-based extraction cannot exceed MAX_HISTORICAL_DAYS and cannot be future-dated."""
        mode = req.get("mode", "").lower()
        today = date.today()

        if mode == "range":
            start_str = req.get("start_date")
            end_str = req.get("end_date")
            if not start_str or not end_str:
                return False, "OPA-401: Range mode requires both start_date and end_date (YYYY-MM-DD)."

            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
                end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
            except ValueError:
                return False, "OPA-402: Invalid date format. Use YYYY-MM-DD."

            if start_dt > end_dt:
                return False, f"OPA-403: Start date ({start_dt}) cannot be after end date ({end_dt})."

            if end_dt > today:
                return False, f"OPA-404: End date ({end_dt}) cannot be in the future (Today is {today})."

            span_days = (end_dt - start_dt).days + 1
            if span_days > self.MAX_HISTORICAL_DAYS:
                return False, (
                    f"OPA-405: Requested date range spans {span_days} days. "
                    f"Maximum allowed historical extraction span is {self.MAX_HISTORICAL_DAYS} days per job."
                )

        elif mode == "single_date":
            single_str = req.get("target_date")
            if not single_str:
                return False, "OPA-406: Single date mode requires target_date (YYYY-MM-DD)."
            try:
                single_dt = datetime.strptime(single_str, "%Y-%m-%d").date()
            except ValueError:
                return False, "OPA-407: Invalid single date format. Use YYYY-MM-DD."

            if single_dt > today:
                return False, f"OPA-408: Target date ({single_dt}) cannot be in the future."

        return True, ""

    def _rule_historical_scope(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        """OPA Rule: Historical month selections must specify valid months and valid years (2024-2026)."""
        mode = req.get("mode", "").lower()
        if mode == "months":
            years = req.get("years", [])
            months = req.get("months", [])
            if not years:
                return False, "OPA-501: At least one year (e.g. 2025, 2026) must be selected."
            if not months:
                return False, "OPA-502: At least one month must be selected for month-based historical fill."

            allowed_years = {2024, 2025, 2026}
            for y in years:
                try:
                    if int(y) not in allowed_years:
                        return False, f"OPA-503: Year {y} is outside supported historical boundary (2024-2026)."
                except (ValueError, TypeError):
                    return False, f"OPA-504: Invalid year format '{y}'."

        return True, ""


def requests_unquote(s: str) -> str:
    import urllib.parse
    return urllib.parse.unquote(s)
