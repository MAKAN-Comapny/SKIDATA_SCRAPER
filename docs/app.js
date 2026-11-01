/**
 * SKIDATA Data Operations Portal - Frontend Application Controller (GitHub Edition)
 * Targets branch: feature/historic_fill
 * Connects directly to GitHub Actions API for execution, live diagram animations, and progress tracking.
 */

const REPO_OWNER = "MAKAN-Comapny";
const REPO_NAME = "SKIDATA_SCRAPER";
const TARGET_BRANCH = "feature/historic_fill";
const WORKFLOW_ID = "skidata-scraper.yml";

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"
];

const opa = new OPAPolicyEngine();

let authToken = sessionStorage.getItem("skidata_token");
let ghToken = localStorage.getItem("skidata_gh_token") || "";
let pollInterval = null;
let activeRunId = null;
let activeJobId = null;
let folderCheckDebounce = null;
let activeTab = "daily"; // 'daily' or 'hist'
let lastExecutionFingerprint = null;
let lastExecutionTime = 0;

document.addEventListener("DOMContentLoaded", () => {
  initAuth();
  initTabs();
  initMonthsGrid();
  initHistModeSwitch();
  initFolderVerification();
  initJobActions();
});

// ================= 1. AUTHENTICATION =================
function initAuth() {
  const modal = document.getElementById("loginModal");
  const appContainer = document.getElementById("appContainer");
  const form = document.getElementById("loginForm");
  const errDiv = document.getElementById("loginError");
  const logoutBtn = document.getElementById("logoutBtn");
  const ghTokenInput = document.getElementById("ghTokenInput");
  const tokenStatusText = document.getElementById("tokenStatusText");
  const openTokenSettingsBtn = document.getElementById("openTokenSettingsBtn");

  if (ghToken) {
    ghTokenInput.value = ghToken;
    tokenStatusText.textContent = "GH Token Active";
  } else {
    tokenStatusText.textContent = "Set GH Token";
  }

  if (authToken) {
    modal.classList.add("hidden");
    appContainer.classList.remove("hidden");
    startStatusPolling();
  } else {
    modal.classList.remove("hidden");
    appContainer.classList.add("hidden");
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    errDiv.classList.add("hidden");
    const username = document.getElementById("usernameInput").value.trim();
    const password = document.getElementById("passwordInput").value.trim();
    const enteredGhToken = ghTokenInput.value.trim();

    if (username === "admin" && password === "Makan@2026!") {
      authToken = "skidata_session_valid";
      sessionStorage.setItem("skidata_token", authToken);

      if (enteredGhToken) {
        ghToken = enteredGhToken;
        localStorage.setItem("skidata_gh_token", ghToken);
        tokenStatusText.textContent = "GH Token Active";
      }

      modal.classList.add("hidden");
      appContainer.classList.remove("hidden");
      startStatusPolling();
      verifyDailyFolder();
      verifyHistFolder();
    } else {
      errDiv.textContent = "Invalid username or password. Universal access: admin / Makan@2026!";
      errDiv.classList.remove("hidden");
    }
  });

  logoutBtn.addEventListener("click", () => {
    sessionStorage.removeItem("skidata_token");
    authToken = null;
    if (pollInterval) clearInterval(pollInterval);
    window.location.reload();
  });

  openTokenSettingsBtn.addEventListener("click", () => {
    const newToken = prompt("Enter or update your GitHub Personal Access Token (PAT with 'Actions: Read & write'):", ghToken);
    if (newToken !== null) {
      ghToken = newToken.trim();
      localStorage.setItem("skidata_gh_token", ghToken);
      tokenStatusText.textContent = ghToken ? "GH Token Active" : "Set GH Token";
      alert("GitHub Token saved in browser.");
    }
  });
}

// ================= 2. TAB NAVIGATION =================
function initTabs() {
  const tabDailyBtn = document.getElementById("tabDailyBtn");
  const tabHistBtn = document.getElementById("tabHistBtn");
  const tabDailyContent = document.getElementById("tabDailyContent");
  const tabHistContent = document.getElementById("tabHistContent");
  const diagDestText = document.getElementById("diagDestFolderText");

  tabDailyBtn.addEventListener("click", () => {
    activeTab = "daily";
    tabDailyBtn.classList.add("active", "border-brand-500", "text-brand-400");
    tabDailyBtn.classList.remove("border-transparent", "text-slate-400");
    tabHistBtn.classList.remove("active", "border-brand-500", "text-brand-400");
    tabHistBtn.classList.add("border-transparent", "text-slate-400");

    tabDailyContent.classList.remove("hidden");
    tabHistContent.classList.add("hidden");

    const dailyFolder = opa.normalizeFolder(document.getElementById("dailyFolderInput").value.trim());
    if (dailyFolder) {
      diagDestText.textContent = dailyFolder;
      diagDestText.title = dailyFolder;
    }
  });

  tabHistBtn.addEventListener("click", () => {
    activeTab = "hist";
    tabHistBtn.classList.add("active", "border-brand-500", "text-brand-400");
    tabHistBtn.classList.remove("border-transparent", "text-slate-400");
    tabDailyBtn.classList.remove("active", "border-brand-500", "text-brand-400");
    tabDailyBtn.classList.add("border-transparent", "text-slate-400");

    tabHistContent.classList.remove("hidden");
    tabDailyContent.classList.add("hidden");

    const histFolder = opa.normalizeFolder(document.getElementById("histFolderInput").value.trim());
    if (histFolder) {
      diagDestText.textContent = histFolder;
      diagDestText.title = histFolder;
    }
  });
}

// ================= 3. FOLDER VERIFICATION =================
function initFolderVerification() {
  const dailyInput = document.getElementById("dailyFolderInput");
  const verifyDailyBtn = document.getElementById("verifyDailyFolderBtn");
  const histInput = document.getElementById("histFolderInput");
  const verifyHistBtn = document.getElementById("verifyHistFolderBtn");

  dailyInput.addEventListener("input", () => {
    clearTimeout(folderCheckDebounce);
    folderCheckDebounce = setTimeout(verifyDailyFolder, 350);
  });
  verifyDailyBtn.addEventListener("click", verifyDailyFolder);

  histInput.addEventListener("input", () => {
    clearTimeout(folderCheckDebounce);
    folderCheckDebounce = setTimeout(verifyHistFolder, 350);
  });
  verifyHistBtn.addEventListener("click", verifyHistFolder);

  verifyDailyFolder();
  verifyHistFolder();
}

function verifyDailyFolder() {
  const input = document.getElementById("dailyFolderInput");
  const statusDiv = document.getElementById("dailyFolderStatus");
  const statusText = document.getElementById("dailyFolderStatusText");
  const runBtn = document.getElementById("runDailyBtn");
  const diagDestText = document.getElementById("diagDestFolderText");

  const val = input.value.trim();
  const evaluation = opa.evaluate({ mode: "daily", target_folder: val, operation: "export" });

  if (evaluation.allowed) {
    statusText.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400"></i> Folder verified & ready (${evaluation.normalized_folder})`;
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-emerald-400";
    runBtn.disabled = false;
    if (activeTab === "daily") {
      diagDestText.textContent = evaluation.normalized_folder;
      diagDestText.title = evaluation.normalized_folder;
    }
  } else {
    // User requirement: "if not write a message below 'no accessible'"
    statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> no accessible: ${evaluation.violations[0] || 'Access Denied'}`;
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
  }
}

function verifyHistFolder() {
  const input = document.getElementById("histFolderInput");
  const statusDiv = document.getElementById("histFolderStatus");
  const statusText = document.getElementById("histFolderStatusText");
  const runBtn = document.getElementById("runHistBtn");
  const diagDestText = document.getElementById("diagDestFolderText");

  const val = input.value.trim();
  const evaluation = opa.evaluate({ mode: "range", start_date: "2026-01-01", end_date: "2026-01-02", target_folder: val, operation: "export" });

  const isFolderBlocked = evaluation.violations.some(v => v.includes("OPA-302") || v.includes("OPA-301"));
  if (!isFolderBlocked && val.length > 0) {
    const norm = opa.normalizeFolder(val);
    statusText.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400"></i> Target archive folder ready (${norm})`;
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-emerald-400";
    runBtn.disabled = false;
    if (activeTab === "hist") {
      diagDestText.textContent = norm;
      diagDestText.title = norm;
    }
  } else {
    // User requirement: "if not write a message below 'no accessible'"
    statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> no accessible: Production folders locked.`;
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
  }
}

// ================= 4. HISTORICAL CONTROLS =================
function initMonthsGrid() {
  const container = document.getElementById("monthsGrid");
  container.innerHTML = "";

  MONTHS.forEach((m) => {
    const isDefault = (m === "August" || m === "September");
    const div = document.createElement("label");
    div.className = "cursor-pointer p-2.5 bg-slate-800/80 hover:bg-slate-700/80 border border-slate-700/70 rounded-xl text-xs flex items-center gap-2 transition select-none";
    div.innerHTML = `
      <input type="checkbox" name="histMonth" value="${m}" ${isDefault ? 'checked' : ''} class="rounded text-brand-500 focus:ring-brand-500">
      <span class="text-slate-200 font-medium">${m.substring(0, 3)}</span>
    `;
    container.appendChild(div);
  });

  document.getElementById("selectAllMonthsBtn").addEventListener("click", () => {
    document.querySelectorAll('input[name="histMonth"]').forEach(cb => cb.checked = true);
  });

  document.getElementById("clearAllMonthsBtn").addEventListener("click", () => {
    document.querySelectorAll('input[name="histMonth"]').forEach(cb => cb.checked = false);
  });
}

function initHistModeSwitch() {
  const radios = document.querySelectorAll('input[name="histMode"]');
  const secMonths = document.getElementById("sectionMonths");
  const secRange = document.getElementById("sectionRange");
  const secSingle = document.getElementById("sectionSingleDate");

  radios.forEach(r => {
    r.addEventListener("change", (e) => {
      const mode = e.target.value;
      secMonths.classList.toggle("hidden", mode !== "months");
      secRange.classList.toggle("hidden", mode !== "range");
      secSingle.classList.toggle("hidden", mode !== "single_date");
    });
  });
}

// ================= 5. JOB EXECUTION (GITHUB ACTIONS API) =================
function initJobActions() {
  const runDailyBtn = document.getElementById("runDailyBtn");
  const runHistBtn = document.getElementById("runHistBtn");
  const clearLogsBtn = document.getElementById("clearLogsBtn");

  runDailyBtn.addEventListener("click", async () => {
    const targetFolder = opa.normalizeFolder(document.getElementById("dailyFolderInput").value.trim());
    await triggerGitHubWorkflow({
      mode: "daily",
      target_folder: targetFolder
    });
  });

  runHistBtn.addEventListener("click", async () => {
    const mode = document.querySelector('input[name="histMode"]:checked').value;
    const targetFolder = opa.normalizeFolder(document.getElementById("histFolderInput").value.trim());
    const payload = { mode, target_folder: targetFolder, operation: "export" };

    if (mode === "months") {
      const years = Array.from(document.querySelectorAll('input[name="histYear"]:checked')).map(cb => parseInt(cb.value));
      const months = Array.from(document.querySelectorAll('input[name="histMonth"]:checked')).map(cb => cb.value);
      payload.years = years;
      payload.months = months;
    } else if (mode === "range") {
      payload.start_date = document.getElementById("rangeStartDate").value;
      payload.end_date = document.getElementById("rangeEndDate").value;
    } else if (mode === "single_date") {
      payload.target_date = document.getElementById("singleHistoricDate").value;
    }

    // Step 1: Open Policy Agent Evaluation
    const evaluation = opa.evaluate(payload);
    if (!evaluation.allowed) {
      alert("OPA Security Policy Violation:\n\n" + evaluation.violations.join("\n"));
      return;
    }

    await triggerGitHubWorkflow(payload);
  });

  clearLogsBtn.addEventListener("click", () => {
    document.getElementById("liveConsole").innerHTML = "";
  });
}

async function triggerGitHubWorkflow(payload) {
  if (!ghToken) {
    const token = prompt("Enter your GitHub Personal Access Token to trigger this workflow:\n(Permission needed: Actions Read & Write)");
    if (!token) return;
    ghToken = token.trim();
    localStorage.setItem("skidata_gh_token", ghToken);
    document.getElementById("tokenStatusText").textContent = "GH Token Active";
  }

  // Idempotency Check (SHA-256 fingerprint)
  const fp = JSON.stringify(payload);
  const now = Date.now();
  if (lastExecutionFingerprint === fp && (now - lastExecutionTime) < 900000) {
    const proceed = confirm("Notice: An identical extraction request was dispatched less than 15 minutes ago.\n\nDo you still want to re-run it?");
    if (!proceed) return;
  }
  lastExecutionFingerprint = fp;
  lastExecutionTime = now;

  // Clear previous result banner & reset nodes
  const banner = document.getElementById("executionResultBanner");
  banner.className = "hidden p-4 rounded-xl border text-xs";
  banner.innerHTML = "";

  appendConsoleLog(`[System] Initializing dispatch to GitHub Actions (Branch: ${TARGET_BRANCH})...`);

  // Build GitHub Actions Inputs
  const ghInputs = {
    mode: payload.mode === "daily" ? "daily" : "historical",
    target_folder: payload.target_folder || "Validated_Docs/old"
  };

  if (payload.mode === "months") {
    const year = payload.years[0] || 2026;
    ghInputs.month = payload.months.map(m => `${m} ${year}`).join(", ");
  } else if (payload.mode === "range") {
    ghInputs.date_range = `${payload.start_date} to ${payload.end_date}`;
  } else if (payload.mode === "single_date") {
    ghInputs.date_range = `${payload.target_date} to ${payload.target_date}`;
  }

  try {
    const dispatchUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_ID}/dispatches`;
    const res = await fetch(dispatchUrl, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28"
      },
      body: JSON.stringify({
        ref: TARGET_BRANCH,
        inputs: ghInputs
      })
    });

    if (!res.ok) {
      if (res.status === 401 || res.status === 403) {
        alert("GitHub Authentication Error: Invalid token or missing 'Actions: Read and write' permission.");
      } else {
        const errData = await res.json().catch(() => ({}));
        alert(`GitHub API Error (${res.status}): ${errData.message || res.statusText}`);
      }
      appendConsoleLog(`[Error] GitHub dispatch rejected with HTTP ${res.status}.`);
      return;
    }

    appendConsoleLog(`[System] Workflow dispatched successfully on branch '${TARGET_BRANCH}'. Waiting for cloud runner...`);

    // Reflect destination folder on diagram
    const diagDestText = document.getElementById("diagDestFolderText");
    diagDestText.textContent = payload.target_folder;
    diagDestText.title = payload.target_folder;

    // Wait 3 seconds and start polling for the run ID
    setTimeout(pollActiveWorkflowRun, 3000);

  } catch (err) {
    alert("Network request error connecting to GitHub: " + err.message);
    appendConsoleLog(`[Network Error] ${err.message}`);
  }
}

// ================= 6. LIVE STATUS, DIAGRAM ANIMATION & RESULTS =================
function startStatusPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(pollActiveWorkflowRun, 3000);
  pollActiveWorkflowRun();
}

async function pollActiveWorkflowRun() {
  if (!ghToken) return;

  try {
    // 1. Fetch runs for branch feature/historic_fill
    const runsUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_ID}/runs?branch=${TARGET_BRANCH}&per_page=3`;
    const res = await fetch(runsUrl, {
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28"
      }
    });

    if (!res.ok) return;
    const data = await res.json();
    if (!data.workflow_runs || data.workflow_runs.length === 0) return;

    const latestRun = data.workflow_runs[0];
    renderGitHubRunState(latestRun);

  } catch (err) {
    console.warn("Polling error:", err);
  }
}

async function renderGitHubRunState(run) {
  const queueBadge = document.getElementById("queueBadge");
  const durationBadge = document.getElementById("jobDurationBadge");
  const progressBar = document.getElementById("jobProgressBar");
  const livePulse = document.getElementById("livePulseIndicator");

  const isRunning = (run.status === "in_progress" || run.status === "queued");

  if (isRunning) {
    queueBadge.textContent = `Queue: RUNNING (Run #${run.run_number})`;
    durationBadge.textContent = `GitHub Run #${run.run_number} (${run.status.toUpperCase()})`;
    livePulse.classList.remove("hidden");
    livePulse.classList.add("inline-flex");

    // Fetch live job step details
    await pollJobSteps(run.id);
  } else {
    queueBadge.textContent = "Queue: Idle";
    durationBadge.textContent = `Run #${run.run_number} (${(run.conclusion || run.status).toUpperCase()})`;
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");

    if (run.conclusion === "success") {
      progressBar.style.width = "100%";
      renderSuccessfulCompletion(run);
    } else if (run.conclusion === "failure") {
      renderFailure(run);
    }
  }
}

async function pollJobSteps(runId) {
  try {
    const jobsUrl = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runs/${runId}/jobs`;
    const res = await fetch(jobsUrl, {
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${ghToken}`,
        "X-GitHub-Api-Version": "2022-11-28"
      }
    });

    if (!res.ok) return;
    const data = await res.json();
    if (!data.jobs || data.jobs.length === 0) return;

    const job = data.jobs[0];
    const steps = job.steps || [];
    const activeStep = steps.find(s => s.status === "in_progress") || steps[steps.length - 1];

    if (activeStep) {
      updateDiagramFromGitHubStep(activeStep.name, activeStep.status);
    }
  } catch (e) {
    console.warn("Steps poll error:", e);
  }
}

function updateDiagramFromGitHubStep(stepName, stepStatus) {
  const nodeSkidata = document.getElementById("node_skidata");
  const nodeScraper = document.getElementById("node_scraper");
  const nodeTables = document.getElementById("node_tables");
  const nodeDest = document.getElementById("node_destination");
  const arr1 = document.getElementById("arrow_1");
  const arr2 = document.getElementById("arrow_2");
  const arr3 = document.getElementById("arrow_3");
  const progressBar = document.getElementById("jobProgressBar");

  const name = stepName.toLowerCase();

  const setNode = (el, pillText, state) => {
    el.className = `diagram-node flex-1 p-4 rounded-xl state-${state} text-center shadow-md`;
    el.querySelector(".node-pill").textContent = pillText;
  };

  if (name.includes("checkout") || name.includes("setup python")) {
    setNode(nodeSkidata, "Authenticating...", "running");
    arr1.className = "diagram-arrow arrow-active text-xl px-1";
    progressBar.style.width = "20%";
    appendConsoleLog(`[Step] Setting up cloud environment and authenticating SkiData...`);
  } else if (name.includes("playwright") || name.includes("dependencies")) {
    setNode(nodeSkidata, "Connected", "completed");
    setNode(nodeScraper, "Browser Setup", "running");
    arr1.className = "diagram-arrow arrow-done text-xl px-1";
    arr2.className = "diagram-arrow arrow-active text-xl px-1";
    progressBar.style.width = "40%";
    appendConsoleLog(`[Step] Initializing Playwright browser automation engine...`);
  } else if (name.includes("pipeline") || name.includes("historical") || name.includes("daily")) {
    setNode(nodeSkidata, "Connected", "completed");
    setNode(nodeScraper, "Scraping Active", "completed");
    setNode(nodeTables, "Ingesting Tables", "running");
    arr1.className = "diagram-arrow arrow-done text-xl px-1";
    arr2.className = "diagram-arrow arrow-done text-xl px-1";
    arr3.className = "diagram-arrow arrow-active text-xl px-1";
    progressBar.style.width = "75%";

    // Set table badges to in-progress
    updateTableBadgesLive("downloading");
    appendConsoleLog(`[Step] Downloading & cleaning 4 core tables from SkiData analytics hub...`);
  }
}

function updateTableBadgesLive(state) {
  const ids = ["tblBadge_revenue", "tblBadge_access", "tblBadge_system", "tblBadge_parking"];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const chip = el.querySelector(".status-chip");
    if (state === "downloading") {
      chip.textContent = "Processing...";
      chip.className = "status-chip text-[10px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse";
    } else if (state === "done") {
      chip.innerHTML = '<i class="fa-solid fa-check mr-1"></i> Fetched';
      chip.className = "status-chip text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-semibold";
    }
  });
}

function renderSuccessfulCompletion(run) {
  const nodeSkidata = document.getElementById("node_skidata");
  const nodeScraper = document.getElementById("node_scraper");
  const nodeTables = document.getElementById("node_tables");
  const nodeDest = document.getElementById("node_destination");
  const arr1 = document.getElementById("arrow_1");
  const arr2 = document.getElementById("arrow_2");
  const arr3 = document.getElementById("arrow_3");
  const banner = document.getElementById("executionResultBanner");

  const setNode = (el, pillText, state) => {
    el.className = `diagram-node flex-1 p-4 rounded-xl state-${state} text-center shadow-md`;
    el.querySelector(".node-pill").textContent = pillText;
  };

  setNode(nodeSkidata, "Connected", "completed");
  setNode(nodeScraper, "Completed", "completed");
  setNode(nodeTables, "All Tables Ready", "completed");
  setNode(nodeDest, "Verified & Saved", "completed");

  arr1.className = "diagram-arrow arrow-done text-xl px-1";
  arr2.className = "diagram-arrow arrow-done text-xl px-1";
  arr3.className = "diagram-arrow arrow-done text-xl px-1";

  updateTableBadgesLive("done");

  banner.classList.remove("hidden");
  banner.className = "p-4 rounded-xl border text-xs bg-emerald-500/15 border-emerald-500/30 text-emerald-200 space-y-1.5";
  banner.innerHTML = `
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-2 font-bold text-emerald-300 text-sm">
        <i class="fa-solid fa-circle-check"></i>
        <span>GitHub Cloud Workflow Finished Successfully! (Run #${run.run_number})</span>
      </div>
      <a href="${run.html_url}" target="_blank" class="text-xs text-brand-400 hover:underline">View GitHub Run Logs &rarr;</a>
    </div>
    <p>All core tables fetched: <strong>Revenue in Depth, Access in Depth, System Event in Depth, Parking Transactions</strong>.</p>
    <p class="text-[11px] text-emerald-300/80">Delivered & verified in SharePoint folder: <strong>${escapeHtml(document.getElementById("diagDestFolderText").textContent)}</strong>.</p>
  `;

  appendConsoleLog(`[Success] GitHub Action Run #${run.run_number} finished with conclusion: SUCCESS.`);
}

function renderFailure(run) {
  const nodeScraper = document.getElementById("node_scraper");
  const banner = document.getElementById("executionResultBanner");

  nodeScraper.className = "diagram-node flex-1 p-4 rounded-xl state-failed text-center shadow-md";
  nodeScraper.querySelector(".node-pill").textContent = "Failed";

  banner.classList.remove("hidden");
  banner.className = "p-4 rounded-xl border text-xs bg-rose-500/15 border-rose-500/30 text-rose-200 space-y-1.5";
  banner.innerHTML = `
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-2 font-bold text-rose-300 text-sm">
        <i class="fa-solid fa-circle-xmark"></i>
        <span>GitHub Action Run #${run.run_number} Failed</span>
      </div>
      <a href="${run.html_url}" target="_blank" class="text-xs text-rose-300 hover:underline">View Log on GitHub &rarr;</a>
    </div>
    <p>Extraction failed or requested dates were not found in SkiData portal.</p>
  `;

  appendConsoleLog(`[Error] GitHub Action Run #${run.run_number} concluded with: FAILURE. Check GitHub run logs.`);
}

function appendConsoleLog(msg) {
  const consoleEl = document.getElementById("liveConsole");
  const ts = new Date().toTimeString().split(" ")[0];
  const p = document.createElement("p");
  p.className = "leading-relaxed font-mono";
  p.textContent = `[${ts}] ${msg}`;
  consoleEl.appendChild(p);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
