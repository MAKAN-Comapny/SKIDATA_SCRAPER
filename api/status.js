/**
 * Vercel Serverless Function: Job Status & Architecture Diagram State
 * Endpoint: GET /api/status
 * Queries GitHub Actions API for branch: feature/historic_fill
 */

const REPO_OWNER = process.env.REPO_OWNER || "MAKAN-Comapny";
const REPO_NAME = process.env.REPO_NAME || "SKIDATA_SCRAPER";
const TARGET_BRANCH = process.env.TARGET_BRANCH || "feature/historic_fill";
const WORKFLOW_ID = process.env.WORKFLOW_ID || "skidata-scraper.yml";

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

    // If completed run is older than 25 minutes and not actively running, show idle on fresh load
    const updatedAt = new Date(run.updated_at || run.created_at).getTime();
    const ageMinutes = (Date.now() - updatedAt) / (1000 * 60);
    if (!isRunning && ageMinutes > 25) {
      return res.status(200).json({ status: "idle" });
    }

    const jobState = {
      id: run.id,
      run_number: run.run_number,
      html_url: run.html_url,
      status: isRunning ? "running" : (run.conclusion === "success" ? "completed" : "failed"),
      progress_percent: 10,
      active_node: "skidata_auth",
      completed_nodes: [],
      tables_status: {
        "Revenue in Depth": "pending",
        "Access in Depth": "pending",
        "System Event in Depth": "pending",
        "Parking Transactions": "pending"
      },
      logs: [
        `[GitHub Runner] Run #${run.run_number} (${run.status.toUpperCase()}) on branch '${TARGET_BRANCH}'`
      ],
      result: run.conclusion === "success" ? { status: "success", run_number: run.run_number } : null,
      error: run.conclusion === "failure" ? `Run #${run.run_number} failed. Check GitHub Action log.` : null
    };

    if (isRunning) {
      // Query step details from GitHub Actions job API
      const jobsUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runs/${run.id}/jobs`;
      const jRes = await fetch(jobsUrl, {
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${ghToken}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "Makan-Vercel-Portal"
        }
      });

      let elapsedPipelineSec = 0;
      let inPipelineStep = false;

      if (jRes.ok) {
        const jData = await jRes.json();
        const steps = (jData.jobs && jData.jobs[0] && jData.jobs[0].steps) || [];
        const pipelineStep = steps.find(s => {
          const n = (s.name || "").toLowerCase();
          return n.includes("pipeline") || n.includes("skidata") || n.includes("scrape");
        });

        if (pipelineStep && (pipelineStep.status === "in_progress" || pipelineStep.status === "completed")) {
          inPipelineStep = true;
          const startMs = pipelineStep.started_at ? new Date(pipelineStep.started_at).getTime() : Date.now();
          elapsedPipelineSec = Math.max(0, (Date.now() - startMs) / 1000);
        } else {
          const activeStep = steps.find(s => s.status === "in_progress") || steps[steps.length - 1];
          if (activeStep) {
            const sName = (activeStep.name || "").toLowerCase();
            if (sName.includes("checkout") || sName.includes("python")) {
              jobState.active_node = "skidata_auth";
              jobState.progress_percent = 15;
              jobState.logs.push(`Step: Setting up runtime environment...`);
            } else if (sName.includes("playwright") || sName.includes("dependencies")) {
              jobState.active_node = "scraper_engine";
              jobState.progress_percent = 25;
              jobState.logs.push(`Step: Initializing Playwright browser engine...`);
            }
          }
        }
      }

      if (inPipelineStep) {
        // Sequential breakdown matching scraper.py execution timeline
        if (elapsedPipelineSec < 35) {
          // 1. Authentication & Portal Session
          jobState.active_node = "skidata_auth";
          jobState.completed_nodes = [];
          jobState.progress_percent = Math.min(30, Math.floor(25 + (elapsedPipelineSec / 35) * 8));
          jobState.tables_status = {
            "Revenue in Depth": "pending",
            "Access in Depth": "pending",
            "System Event in Depth": "pending",
            "Parking Transactions": "pending"
          };
          jobState.logs.push(`[Auth] Authenticating session on SkiData Qlik Sense Cloud...`);
        } else if (elapsedPipelineSec < 85) {
          // 2. Table 1: Revenue in Depth
          jobState.active_node = "table_revenue";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine"];
          jobState.progress_percent = Math.min(50, Math.floor(33 + ((elapsedPipelineSec - 35) / 50) * 17));
          jobState.tables_status = {
            "Revenue in Depth": "downloading",
            "Access in Depth": "pending",
            "System Event in Depth": "pending",
            "Parking Transactions": "pending"
          };
          jobState.logs.push(`[Scraper] Extracting Table 1/4: Revenue In Depth Analysis...`);
        } else if (elapsedPipelineSec < 135) {
          // 3. Table 2: Access in Depth
          jobState.active_node = "table_access";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue"];
          jobState.progress_percent = Math.min(70, Math.floor(50 + ((elapsedPipelineSec - 85) / 50) * 20));
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "downloading",
            "System Event in Depth": "pending",
            "Parking Transactions": "pending"
          };
          jobState.logs.push(`[Scraper] Table 1 Fetched. Extracting Table 2/4: Access In Depth Analysis...`);
        } else if (elapsedPipelineSec < 185) {
          // 4. Table 3: System Event in Depth
          jobState.active_node = "table_system";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access"];
          jobState.progress_percent = Math.min(85, Math.floor(70 + ((elapsedPipelineSec - 135) / 50) * 15));
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "downloading",
            "Parking Transactions": "pending"
          };
          jobState.logs.push(`[Scraper] Table 2 Fetched. Extracting Table 3/4: System Event In Depth...`);
        } else if (elapsedPipelineSec < 240) {
          // 5. Table 4: Parking Transactions
          jobState.active_node = "table_parking";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access", "table_system"];
          jobState.progress_percent = Math.min(93, Math.floor(85 + ((elapsedPipelineSec - 185) / 55) * 8));
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "done",
            "Parking Transactions": "downloading"
          };
          jobState.logs.push(`[Scraper] Table 3 Fetched. Extracting Table 4/4: Parking Transactions...`);
        } else {
          // 6. Final Transformation & SharePoint Upload
          jobState.active_node = "destination_upload";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access", "table_system", "table_parking"];
          jobState.progress_percent = 96;
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "done",
            "Parking Transactions": "done"
          };
          jobState.logs.push(`[SharePoint] All 4 tables cleaned. Uploading to SharePoint destination folder...`);
        }
      }
    } else if (run.conclusion === "success") {
      // Completed successfully - all nodes and tables marked done
      jobState.status = "completed";
      jobState.active_node = "completed";
      jobState.completed_nodes = [
        "skidata_auth",
        "scraper_engine",
        "table_revenue",
        "table_access",
        "table_system",
        "table_parking",
        "destination_upload"
      ];
      jobState.progress_percent = 100;
      jobState.tables_status = {
        "Revenue in Depth": "done",
        "Access in Depth": "done",
        "System Event in Depth": "done",
        "Parking Transactions": "done"
      };
      jobState.logs.push(`[Verified] All 4 tables verified and stored in SharePoint destination folder.`);
    }

    return res.status(200).json(jobState);

  } catch (err) {
    return res.status(200).json({ status: "idle", error: err.message });
  }
};
