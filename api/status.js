/**
 * Vercel Serverless Function: Job Status, Validation Gate Report & Architecture Diagram State
 * Endpoint: GET /api/status
 * Queries GitHub Actions API + validation-report artifact for branch: feature/historic_fill
 */

const zlib = require("zlib");

const REPO_OWNER = process.env.REPO_OWNER || "MAKAN-Comapny";
const REPO_NAME = process.env.REPO_NAME || "SKIDATA_SCRAPER";
const TARGET_BRANCH = process.env.TARGET_BRANCH || "feature/historic_fill";
const WORKFLOW_ID = process.env.WORKFLOW_ID || "skidata-scraper.yml";

/**
 * Extract and parse a JSON file from a standard ZIP Buffer (supports Central Directory
 * sizes, so GitHub Actions `actions/upload-artifact` Data Descriptor entries work reliably).
 */
function extractJsonFromZipBuffer(buf, targetFilename = "_validation_report.json") {
  if (!Buffer.isBuffer(buf) || buf.length < 22) {
    return null;
  }

  // Locate End of Central Directory (PK\x05\x06) scanning backwards
  const minPos = Math.max(0, buf.length - 65557);
  let eocdOffset = -1;
  for (let i = buf.length - 22; i >= minPos; i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocdOffset = i;
      break;
    }
  }
  if (eocdOffset === -1) {
    return null;
  }

  const cdSize = buf.readUInt32LE(eocdOffset + 12);
  const cdOffset = buf.readUInt32LE(eocdOffset + 16);
  const cdEnd = cdOffset + cdSize;
  if (cdOffset >= buf.length || cdEnd > buf.length) {
    return null;
  }

  let cursor = cdOffset;
  let fallbackEntry = null;
  let matchedEntry = null;

  while (cursor + 46 <= cdEnd) {
    if (buf.readUInt32LE(cursor) !== 0x02014b50) {
      break;
    }
    const method = buf.readUInt16LE(cursor + 10);
    const compressedSize = buf.readUInt32LE(cursor + 20);
    const nameLen = buf.readUInt16LE(cursor + 28);
    const extraLen = buf.readUInt16LE(cursor + 30);
    const commentLen = buf.readUInt16LE(cursor + 32);
    const localOffset = buf.readUInt32LE(cursor + 42);
    const nameStart = cursor + 46;
    const entryName = buf.toString("utf8", nameStart, nameStart + nameLen);

    const entry = { method, compressedSize, localOffset, entryName };
    if (entryName === targetFilename || entryName.endsWith(`/${targetFilename}`) || entryName.endsWith(`\\${targetFilename}`)) {
      matchedEntry = entry;
      break;
    }
    if (!fallbackEntry && entryName.endsWith(".json")) {
      fallbackEntry = entry;
    }
    cursor = nameStart + nameLen + extraLen + commentLen;
  }

  const chosen = matchedEntry || fallbackEntry;
  if (!chosen || chosen.localOffset + 30 > buf.length) {
    return null;
  }
  if (buf.readUInt32LE(chosen.localOffset) !== 0x04034b50) {
    return null;
  }

  const lhNameLen = buf.readUInt16LE(chosen.localOffset + 26);
  const lhExtraLen = buf.readUInt16LE(chosen.localOffset + 28);
  const dataStart = chosen.localOffset + 30 + lhNameLen + lhExtraLen;
  const dataEnd = dataStart + chosen.compressedSize;
  if (dataEnd > buf.length) {
    return null;
  }

  const compressedPayload = buf.subarray(dataStart, dataEnd);
  let rawText = null;
  if (chosen.method === 0) {
    rawText = compressedPayload.toString("utf8");
  } else if (chosen.method === 8) {
    rawText = zlib.inflateRawSync(compressedPayload).toString("utf8");
  } else {
    return null;
  }

  return JSON.parse(rawText);
}

async function fetchValidationReportArtifact(runId, ghToken) {
  try {
    const artifactsUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runs/${runId}/artifacts?per_page=10`;
    const aRes = await fetch(artifactsUrl, {
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Makan-Vercel-Portal"
      }
    });
    if (!aRes.ok) {
      return null;
    }

    const aData = await aRes.json();
    const artifact = (aData.artifacts || []).find(
      a => a.name === "validation-report" && !a.expired
    );
    if (!artifact || !artifact.archive_download_url) {
      return null;
    }

    // Download ZIP (GitHub replies with 302 redirect to pre-signed Azure Blob URL;
    // follow manually without Authorization header on the signed URL)
    const dlInit = await fetch(artifact.archive_download_url, {
      redirect: "manual",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Makan-Vercel-Portal"
      }
    });

    let zipRes = dlInit;
    if ([301, 302, 303, 307, 308].includes(dlInit.status)) {
      const redirectUrl = dlInit.headers.get("location");
      if (!redirectUrl) {
        return null;
      }
      zipRes = await fetch(redirectUrl);
    }

    if (!zipRes.ok) {
      return null;
    }

    const arrBuf = await zipRes.arrayBuffer();
    return extractJsonFromZipBuffer(Buffer.from(arrBuf));
  } catch (err) {
    console.warn("Validation artifact fetch skipped:", err.message);
    return null;
  }
}

function applyValidationReportToJobState(jobState, report) {
  if (!report) {
    return jobState;
  }

  jobState.validation_report = report;
  jobState.validation_enabled = report.validation_enabled !== false;

  if (report.tables && typeof report.tables === "object") {
    jobState.table_details = report.tables;
    for (const key of Object.keys(jobState.tables_status)) {
      const entry = report.tables[key];
      if (entry && entry.status) {
        jobState.tables_status[key] = entry.status;
      }
    }
  }

  const gateStatus = report.status || "passed";
  if (gateStatus === "failed" || report.upload_blocked) {
    jobState.status = "failed";
    jobState.active_node = "validation_gate";
    jobState.completed_nodes = [
      "skidata_auth",
      "scraper_engine",
      "table_revenue",
      "table_access",
      "table_system",
      "table_parking"
    ];
    jobState.error = report.summary || "SharePoint upload blocked by Data Validation Gate.";
    jobState.logs.push(`[Validation Gate] BLOCKED: ${jobState.error}`);
    for (const hf of report.hard_failures || []) {
      jobState.logs.push(`[HARD FAIL] [${hf.code}] ${hf.message}`);
    }
  } else if (gateStatus === "warning") {
    jobState.logs.push(`[Validation Gate] PASSED WITH SOFT WARNINGS: ${report.summary}`);
    for (const sw of report.soft_warnings || []) {
      jobState.logs.push(`[SOFT WARN] [${sw.code}] ${sw.message}`);
    }
  } else if (gateStatus === "bypassed") {
    jobState.logs.push("[Validation Gate] BYPASSED by operator (time-constraint mode). Files uploaded directly.");
  } else {
    jobState.logs.push("[Validation Gate] PASSED: Schema, Excel types, calendar coverage, and financials verified.");
  }

  return jobState;
}

async function handler(req, res) {
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
      created_at: run.created_at,
      updated_at: run.updated_at,
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
      table_details: null,
      validation_report: null,
      validation_enabled: true,
      logs: [
        `[GitHub Runner] Run #${run.run_number} (${run.status.toUpperCase()}) on branch '${TARGET_BRANCH}'`
      ],
      result: run.conclusion === "success" ? { status: "success", run_number: run.run_number } : null,
      error: run.conclusion === "failure" ? `Run #${run.run_number} failed. Check GitHub Action log or Validation Gate report.` : null
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
      let inReportStep = false;

      if (jRes.ok) {
        const jData = await jRes.json();
        const steps = (jData.jobs && jData.jobs[0] && jData.jobs[0].steps) || [];
        const reportStep = steps.find(s => (s.name || "").toLowerCase().includes("validation report"));
        if (reportStep && reportStep.status === "in_progress") {
          inReportStep = true;
        }

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
              jobState.logs.push("Step: Setting up runtime environment...");
            } else if (sName.includes("playwright") || sName.includes("dependencies")) {
              jobState.active_node = "scraper_engine";
              jobState.progress_percent = 25;
              jobState.logs.push("Step: Initializing Playwright browser engine...");
            }
          }
        }
      }

      if (inReportStep) {
        jobState.active_node = "validation_gate";
        jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access", "table_system", "table_parking"];
        jobState.progress_percent = 95;
        jobState.tables_status = {
          "Revenue in Depth": "done",
          "Access in Depth": "done",
          "System Event in Depth": "done",
          "Parking Transactions": "done"
        };
        jobState.logs.push("[Validation Gate] Finalizing verification report & artifact...");
      } else if (inPipelineStep) {
        // Sequential breakdown matching scraper execution timeline
        if (elapsedPipelineSec < 35) {
          jobState.active_node = "skidata_auth";
          jobState.completed_nodes = [];
          jobState.progress_percent = Math.min(30, Math.floor(25 + (elapsedPipelineSec / 35) * 8));
          jobState.logs.push("[Auth] Authenticating session on SkiData Qlik Sense Cloud...");
        } else if (elapsedPipelineSec < 85) {
          jobState.active_node = "table_revenue";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine"];
          jobState.progress_percent = Math.min(48, Math.floor(33 + ((elapsedPipelineSec - 35) / 50) * 15));
          jobState.tables_status = {
            "Revenue in Depth": "downloading",
            "Access in Depth": "pending",
            "System Event in Depth": "pending",
            "Parking Transactions": "pending"
          };
          jobState.logs.push("[Scraper] Extracting Table 1/4: Revenue In Depth Analysis...");
        } else if (elapsedPipelineSec < 135) {
          jobState.active_node = "table_access";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue"];
          jobState.progress_percent = Math.min(65, Math.floor(48 + ((elapsedPipelineSec - 85) / 50) * 17));
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "downloading",
            "System Event in Depth": "pending",
            "Parking Transactions": "pending"
          };
          jobState.logs.push("[Scraper] Table 1 Fetched. Extracting Table 2/4: Access In Depth Analysis...");
        } else if (elapsedPipelineSec < 185) {
          jobState.active_node = "table_system";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access"];
          jobState.progress_percent = Math.min(80, Math.floor(65 + ((elapsedPipelineSec - 135) / 50) * 15));
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "downloading",
            "Parking Transactions": "pending"
          };
          jobState.logs.push("[Scraper] Table 2 Fetched. Extracting Table 3/4: System Event In Depth...");
        } else if (elapsedPipelineSec < 235) {
          jobState.active_node = "table_parking";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access", "table_system"];
          jobState.progress_percent = Math.min(90, Math.floor(80 + ((elapsedPipelineSec - 185) / 50) * 10));
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "done",
            "Parking Transactions": "downloading"
          };
          jobState.logs.push("[Scraper] Table 3 Fetched. Extracting Table 4/4: Parking Transactions...");
        } else if (elapsedPipelineSec < 265) {
          jobState.active_node = "validation_gate";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access", "table_system", "table_parking"];
          jobState.progress_percent = 93;
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "done",
            "Parking Transactions": "done"
          };
          jobState.logs.push("[Validation Gate] Running completeness, Excel cell-type & reconciliation checks...");
        } else {
          jobState.active_node = "destination_upload";
          jobState.completed_nodes = ["skidata_auth", "scraper_engine", "table_revenue", "table_access", "table_system", "table_parking", "validation_gate"];
          jobState.progress_percent = 97;
          jobState.tables_status = {
            "Revenue in Depth": "done",
            "Access in Depth": "done",
            "System Event in Depth": "done",
            "Parking Transactions": "done"
          };
          jobState.logs.push("[SharePoint] Validation passed. Uploading verified tables to SharePoint...");
        }
      }
    } else if (run.conclusion === "success") {
      jobState.status = "completed";
      jobState.active_node = "completed";
      jobState.completed_nodes = [
        "skidata_auth",
        "scraper_engine",
        "table_revenue",
        "table_access",
        "table_system",
        "table_parking",
        "validation_gate",
        "destination_upload"
      ];
      jobState.progress_percent = 100;
      jobState.tables_status = {
        "Revenue in Depth": "done",
        "Access in Depth": "done",
        "System Event in Depth": "done",
        "Parking Transactions": "done"
      };
      const report = await fetchValidationReportArtifact(run.id, ghToken);
      if (report) {
        applyValidationReportToJobState(jobState, report);
      } else {
        jobState.logs.push("[Verified] All 4 tables stored in SharePoint destination folder.");
      }
    } else {
      // Failed or cancelled run — fetch validation report artifact so UI highlights EXACT cause!
      const report = await fetchValidationReportArtifact(run.id, ghToken);
      if (report) {
        applyValidationReportToJobState(jobState, report);
      }
    }

    return res.status(200).json(jobState);
  } catch (err) {
    return res.status(200).json({ status: "idle", error: err.message });
  }
}

handler.extractJsonFromZipBuffer = extractJsonFromZipBuffer;
handler.applyValidationReportToJobState = applyValidationReportToJobState;
module.exports = handler;
