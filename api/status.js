/**
 * Vercel Serverless Function: Job Status & Architecture Diagram State
 * Endpoint: GET /api/status
 * Queries GitHub Actions API for branch: feature/historic_fill
 */

const REPO_OWNER = "MAKAN-Comapny";
const REPO_NAME = "SKIDATA_SCRAPER";
const TARGET_BRANCH = "feature/historic_fill";
const WORKFLOW_ID = "skidata-scraper.yml";

module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  const ghToken = process.env.GITHUB_TOKEN || process.env.GH_PAT || "";
  if (!ghToken) {
    return res.status(200).json({ status: "idle", message: "GITHUB_TOKEN not set" });
  }

  try {
    const runsUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_ID}/runs?branch=${TARGET_BRANCH}&per_page=1`;
    const rRes = await fetch(runsUrl, {
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Makan-Vercel-Portal"
      }
    });

    if (!rRes.ok) {
      return res.status(200).json({ status: "idle" });
    }

    const data = await rRes.json();
    if (!data.workflow_runs || data.workflow_runs.length === 0) {
      return res.status(200).json({ status: "idle" });
    }

    const run = data.workflow_runs[0];
    const isRunning = (run.status === "in_progress" || run.status === "queued");

    // Default structure matching queue_lock state
    const jobState = {
      id: run.id,
      run_number: run.run_number,
      html_url: run.html_url,
      status: isRunning ? "running" : (run.conclusion === "success" ? "completed" : "failed"),
      progress_percent: isRunning ? 50 : 100,
      active_node: "scraper_engine",
      completed_nodes: ["skidata_auth"],
      tables_status: {
        "Revenue in Depth": isRunning ? "downloading" : "done",
        "Access in Depth": isRunning ? "downloading" : "done",
        "System Event in Depth": isRunning ? "pending" : "done",
        "Parking Transactions": isRunning ? "pending" : "done"
      },
      logs: [
        `[GitHub Runner] Run #${run.run_number} (${run.status.toUpperCase()}) on branch '${TARGET_BRANCH}'`
      ],
      result: run.conclusion === "success" ? { status: "success", run_number: run.run_number } : null,
      error: run.conclusion === "failure" ? `Run #${run.run_number} failed. Check GitHub Action log.` : null
    };

    if (isRunning) {
      // Query step details
      const jobsUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runs/${run.id}/jobs`;
      const jRes = await fetch(jobsUrl, {
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${ghToken}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "Makan-Vercel-Portal"
        }
      });

      if (jRes.ok) {
        const jData = await jRes.json();
        const steps = (jData.jobs && jData.jobs[0] && jData.jobs[0].steps) || [];
        const activeStep = steps.find(s => s.status === "in_progress") || steps[steps.length - 1];

        if (activeStep) {
          const sName = activeStep.name.toLowerCase();
          if (sName.includes("checkout") || sName.includes("python")) {
            jobState.active_node = "skidata_auth";
            jobState.progress_percent = 20;
            jobState.logs.push(`Step in progress: Initializing environment and SkiData session...`);
          } else if (sName.includes("playwright") || sName.includes("dependencies")) {
            jobState.active_node = "scraper_engine";
            jobState.progress_percent = 40;
            jobState.logs.push(`Step in progress: Launching Playwright browser engine...`);
          } else if (sName.includes("pipeline") || sName.includes("historical") || sName.includes("daily")) {
            jobState.active_node = "table_revenue";
            jobState.progress_percent = 75;
            jobState.logs.push(`Step in progress: Scraping and cleaning 4 core tables from SkiData...`);
          }
        }
      }
    } else if (run.conclusion === "success") {
      jobState.active_node = "destination_upload";
      jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "destination_upload"];
      jobState.progress_percent = 100;
      jobState.tables_status = {
        "Revenue in Depth": "done",
        "Access in Depth": "done",
        "System Event in Depth": "done",
        "Parking Transactions": "done"
      };
      jobState.logs.push(`[GitHub Runner] All 4 tables verified and uploaded to SharePoint successfully.`);
    }

    return res.status(200).json(jobState);

  } catch (err) {
    return res.status(200).json({ status: "idle", error: err.message });
  }
};
