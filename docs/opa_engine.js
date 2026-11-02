/**
 * Open Policy Agent (OPA) Rules Engine - Client-Side Verification
 * Evaluates enterprise guardrails, folder safety, read-only guarantees, and date limits before dispatching to GitHub Actions.
 */

class OPAPolicyEngine {
  constructor() {
    this.FORBIDDEN_FOLDERS = [
      "skidata_daily_files",
      "skidata-daily-files",
      "skidata - odoo",
      "skidata_odoo",
      "ski-data-daily"
    ];
    this.ALLOWED_ROOTS = [
      "validated_docs",
      "ski data 5",
      "odoo_testing_sumit",
      "validation_docs",
      "odoo_aug_sep"
    ];
    this.MAX_HISTORICAL_DAYS = 62;
    this.ALLOWED_YEARS = [2024, 2025, 2026];
  }

  normalizeFolder(folderStr) {
    if (!folderStr) return "Validated_Docs/old";
    let clean = folderStr.trim();

    // Handle full SharePoint URL
    if (clean.includes("sharepoint.com")) {
      const match = clean.match(/Shared%20Documents\/([^?#]+)/i);
      if (match) {
        clean = decodeURIComponent(match[1]);
      } else {
        const match2 = clean.match(/sites\/[^/]+\/([^?#]+)/i);
        if (match2) {
          clean = decodeURIComponent(match2[1]);
        }
      }
    }
    return clean.replace(/^[/\\]+/, "").replace(/[/\\]+$/, "");
  }

  evaluate(payload) {
    const violations = [];

    // 1. Read-Only Safety Protocol
    const operation = (payload.operation || "export").toLowerCase();
    if (!["export", "scrape", "read", "fetch"].includes(operation)) {
      violations.push("OPA-201: Forbidden operation. Only strictly READ-ONLY data export is permitted.");
    }

    // 2. Folder Safety Guardrail
    const targetFolder = this.normalizeFolder(payload.target_folder || "");
    if (!targetFolder) {
      violations.push("OPA-301: Target destination folder must be specified.");
    } else {
      const norm = targetFolder.toLowerCase();
      const rootSeg = norm.split(/[/|\\]/)[0].trim();
      if (this.FORBIDDEN_FOLDERS.some(f => rootSeg === f || norm === f)) {
        violations.push(
          `OPA-302: Safety Guardrail Triggered - Writing to production folder '${targetFolder}' is locked. Staging folders (e.g. Validated_Docs/old) must be used.`
        );
      } else if (!this.ALLOWED_ROOTS.some(r => rootSeg === r)) {
        violations.push(
          `OPA-303: Folder '${targetFolder}' does not exist in SharePoint document library. Permitted folders must be inside 'Validated_Docs' or 'Ski Data 5'.`
        );
      }
    }

    // 3. Date Range Limits
    const today = new Date();
    today.setHours(23, 59, 59, 999);

    if (payload.mode === "range") {
      const { start_date, end_date } = payload;
      if (!start_date || !end_date) {
        violations.push("OPA-401: Range mode requires both start_date and end_date (YYYY-MM-DD).");
      } else {
        const startDt = new Date(start_date);
        const endDt = new Date(end_date);

        if (isNaN(startDt.getTime()) || isNaN(endDt.getTime())) {
          violations.push("OPA-402: Invalid date format. Use YYYY-MM-DD.");
        } else {
          if (startDt > endDt) {
            violations.push(`OPA-403: Start date (${start_date}) cannot be after end date (${end_date}).`);
          }
          if (endDt > today) {
            violations.push(`OPA-404: End date (${end_date}) cannot be in the future.`);
          }
          const diffDays = Math.round((endDt - startDt) / (1000 * 60 * 60 * 24)) + 1;
          if (diffDays > this.MAX_HISTORICAL_DAYS) {
            violations.push(
              `OPA-405: Date range spans ${diffDays} days. Maximum allowed historical extraction span is ${this.MAX_HISTORICAL_DAYS} days per job.`
            );
          }
        }
      }
    } else if (payload.mode === "single_date") {
      const { target_date } = payload;
      if (!target_date) {
        violations.push("OPA-406: Single date mode requires target_date (YYYY-MM-DD).");
      } else {
        const singleDt = new Date(target_date);
        if (isNaN(singleDt.getTime())) {
          violations.push("OPA-407: Invalid date format. Use YYYY-MM-DD.");
        } else if (singleDt > today) {
          violations.push(`OPA-408: Target date (${target_date}) cannot be in the future.`);
        }
      }
    }

    // 4. Historical Scope (Years & Months)
    if (payload.mode === "months") {
      const years = payload.years || [];
      const months = payload.months || [];

      if (years.length === 0) {
        violations.push("OPA-501: At least one year (e.g. 2025, 2026) must be selected.");
      }
      if (months.length === 0) {
        violations.push("OPA-502: At least one month must be selected for month-based historical fill.");
      }
      for (const y of years) {
        if (!this.ALLOWED_YEARS.includes(Number(y))) {
          violations.push(`OPA-503: Year ${y} is outside supported historical boundary (2024-2026).`);
        }
      }
    }

    return {
      allowed: violations.length === 0,
      violations: violations,
      normalized_folder: targetFolder
    };
  }
}
