/**
 * Vercel Serverless Function: Execute Extraction via GitHub Actions Dispatch
 * Endpoint: POST /api/execute
 * Targets branch: feature/historic_fill
 */

const REPO_OWNER = process.env.REPO_OWNER || "MAKAN-Comapny";
const REPO_NAME = process.env.REPO_NAME || "SKIDATA_SCRAPER";
const TARGET_BRANCH = process.env.TARGET_BRANCH || "feature/historic_fill";
const WORKFLOW_ID = process.env.WORKFLOW_ID || "skidata-scraper.yml";

const FORBIDDEN_FOLDERS = [
  "skidata_daily_files",
  "skidata-daily-files",
  "skidata - odoo",
  "skidata_odoo",
  "ski-data-daily"
];

function evaluateOPA(payload) {
  const violations = [];

  // Read-only check
  const operation = (payload.operation || "export").toLowerCase();
  if (!["export", "scrape", "read", "fetch"].includes(operation)) {
    violations.push("OPA-201: Forbidden operation. Only strictly READ-ONLY data export is permitted.");
  }

  // Folder guardrail check
  const folder = String(payload.target_folder || "").trim().toLowerCase();
  const rootSeg = folder.split(/[/|\\]/)[0].trim();
  if (FORBIDDEN_FOLDERS.some(f => rootSeg === f || folder === f)) {
    violations.push("OPA-302: Safety Guardrail Triggered - Production daily folders are locked.");
  }

  // Date range limits
  const today = new Date();
  today.setHours(23, 59, 59, 999);

  if (payload.mode === "range") {
    const { start_date, end_date } = payload;
    if (!start_date || !end_date) {
      violations.push("OPA-401: Range mode requires both start_date and end_date.");
    } else {
      const startDt = new Date(start_date);
      const endDt = new Date(end_date);
      if (startDt > endDt) {
        violations.push("OPA-403: Start date cannot be after end date.");
      }
      if (endDt > today) {
        violations.push("OPA-404: End date cannot be in the future.");
      }
      const diff = Math.round((endDt - startDt) / (1000 * 60 * 60 * 24)) + 1;
      if (diff > 62) {
        violations.push("OPA-405: Maximum historical extraction range is 62 days per job.");
      }
    }
  } else if (payload.mode === "single_date") {
    const { target_date } = payload;
    if (!target_date) {
      violations.push("OPA-406: Single date mode requires target_date.");
    } else {
      const singleDt = new Date(target_date);
      if (singleDt > today) {
        violations.push("OPA-408: Target date cannot be in the future.");
      }
    }
  }

  // Historical scope
  if (payload.mode === "months") {
    const years = payload.years || [];
    const months = payload.months || [];
    if (years.length === 0 || months.length === 0) {
      violations.push("OPA-501: At least one year and one month must be selected.");
    }
    const allowedYears = [2024, 2025, 2026];
    for (const y of years) {
      if (!allowedYears.includes(Number(y))) {
        violations.push(`OPA-503: Year ${y} is outside supported historical boundary (2024-2026).`);
      }
    }
  }

  return { allowed: violations.length === 0, violations };
}

module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const payload = req.body || {};
  const opaResult = evaluateOPA(payload);

  if (!opaResult.allowed) {
    return res.status(403).json({
      detail: {
        error: "OPA Policy Violation: Request blocked by enterprise safety rules.",
        violations: opaResult.violations
      }
    });
  }

  const ghToken = process.env.GITHUB_TOKEN || process.env.GH_PAT || "";
  if (!ghToken) {
    return res.status(500).json({
      detail: "GITHUB_TOKEN is not configured in Vercel environment variables."
    });
  }

  // Build GitHub Actions input parameters
  const ghInputs = {
    mode: payload.mode === "daily" ? "daily" : "historical",
    target_folder: payload.target_folder || "Validated_Docs/old"
  };

  if (payload.mode === "months") {
    const year = (payload.years && payload.years[0]) || 2026;
    ghInputs.month = (payload.months || []).map(m => `${m} ${year}`).join(", ");
  } else if (payload.mode === "range") {
    ghInputs.date_range = `${payload.start_date} to ${payload.end_date}`;
  } else if (payload.mode === "single_date") {
    ghInputs.date_range = `${payload.target_date} to ${payload.target_date}`;
  }

  try {
    const dispatchUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_ID}/dispatches`;
    const ghRes = await fetch(dispatchUrl, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Makan-Vercel-Portal"
      },
      body: JSON.stringify({
        ref: TARGET_BRANCH,
        inputs: ghInputs
      })
    });

    if (!ghRes.ok) {
      const errText = await ghRes.text();
      return res.status(ghRes.status).json({
        detail: `GitHub Dispatch Error (${ghRes.status}): ${errText}`
      });
    }

    return res.status(200).json({
      success: true,
      message: `Extraction workflow launched on branch '${TARGET_BRANCH}'. Cloud runner is executing.`,
      job: {
        mode: payload.mode,
        target_folder: ghInputs.target_folder,
        branch: TARGET_BRANCH
      }
    });

  } catch (err) {
    return res.status(500).json({
      detail: "Failed to dispatch workflow: " + err.message
    });
  }
};
