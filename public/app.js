/**
 * SKIDATA Data Operations Portal - Frontend Application Controller (Vercel Edition)
 * Built for Seniors & Management: Zero tokens or code needed.
 */

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"
];

let authToken = sessionStorage.getItem("skidata_token");
let pollInterval = null;
let folderCheckDebounce = null;
let activeTab = "daily"; // 'daily' or 'hist'

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

  if (authToken) {
    modal.classList.add("hidden");
    appContainer.classList.remove("hidden");
    startStatusPolling();
  } else {
    modal.classList.remove("hidden");
    appContainer.classList.add("hidden");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errDiv.classList.add("hidden");
    const username = document.getElementById("usernameInput").value.trim();
    const password = document.getElementById("passwordInput").value.trim();

    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Invalid username or password.");
      }

      const data = await res.json();
      authToken = data.token;
      sessionStorage.setItem("skidata_token", authToken);

      modal.classList.add("hidden");
      appContainer.classList.remove("hidden");
      startStatusPolling();
      verifyDailyFolder();
      verifyHistFolder();
    } catch (err) {
      errDiv.textContent = err.message;
      errDiv.classList.remove("hidden");
    }
  });

  logoutBtn.addEventListener("click", () => {
    sessionStorage.removeItem("skidata_token");
    authToken = null;
    if (pollInterval) clearInterval(pollInterval);
    window.location.reload();
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

    const dailyFolder = document.getElementById("dailyFolderInput").value.trim();
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

    const histFolder = document.getElementById("histFolderInput").value.trim();
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

async function verifyDailyFolder() {
  const input = document.getElementById("dailyFolderInput");
  const statusDiv = document.getElementById("dailyFolderStatus");
  const statusText = document.getElementById("dailyFolderStatusText");
  const runBtn = document.getElementById("runDailyBtn");
  const diagDestText = document.getElementById("diagDestFolderText");

  const val = input.value.trim();
  if (!val) {
    statusText.textContent = "no accessible: folder path cannot be empty";
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
    return;
  }

  try {
    const res = await fetch("/api/verify-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_input: val })
    });
    const data = await res.json();

    if (data.accessible) {
      input.dataset.cleanPath = data.path;
      statusText.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400"></i> SharePoint folder verified: <strong>${data.path}</strong>`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-emerald-400";
      runBtn.disabled = false;
      diagDestText.textContent = data.path;
      diagDestText.title = data.path;
    } else {
      // User requirement: "if not write a message below 'no accessible'"
      const err = data.error || 'Access Denied';
      const msg = err.startsWith('no accessible') ? err : `no accessible: ${err}`;
      statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> ${msg}`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
      runBtn.disabled = true;
    }
  } catch (err) {
    statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> no accessible`;
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
  }
}

async function verifyHistFolder() {
  const input = document.getElementById("histFolderInput");
  const statusDiv = document.getElementById("histFolderStatus");
  const statusText = document.getElementById("histFolderStatusText");
  const runBtn = document.getElementById("runHistBtn");
  const diagDestText = document.getElementById("diagDestFolderText");

  const val = input.value.trim();
  if (!val) {
    statusText.textContent = "no accessible: folder path cannot be empty";
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
    return;
  }

  try {
    const res = await fetch("/api/verify-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_input: val })
    });
    const data = await res.json();

    if (data.accessible) {
      input.dataset.cleanPath = data.path;
      statusText.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400"></i> SharePoint folder verified: <strong>${data.path}</strong>`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-emerald-400";
      runBtn.disabled = false;
      diagDestText.textContent = data.path;
      diagDestText.title = data.path;
    } else {
      // User requirement: "if not write a message below 'no accessible'"
      const err = data.error || 'Access Denied';
      const msg = err.startsWith('no accessible') ? err : `no accessible: ${err}`;
      statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> ${msg}`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
      runBtn.disabled = true;
    }
  } catch (err) {
    statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> no accessible`;
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

  // 1-Click calendar picker popup
  ['rangeStartDate', 'rangeEndDate', 'singleHistoricDate'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("click", () => {
        if (typeof el.showPicker === "function") {
          try { el.showPicker(); } catch (err) {}
        }
      });
    }
  });
}

// ================= 5. JOB EXECUTION =================
function initJobActions() {
  const runDailyBtn = document.getElementById("runDailyBtn");
  const runHistBtn = document.getElementById("runHistBtn");
  const clearLogsBtn = document.getElementById("clearLogsBtn");

  runDailyBtn.addEventListener("click", async () => {
    const dailyInput = document.getElementById("dailyFolderInput");
    const targetFolder = dailyInput.dataset.cleanPath || dailyInput.value.trim();
    await triggerExecution({
      mode: "daily",
      target_folder: targetFolder
    });
  });

  runHistBtn.addEventListener("click", async () => {
    const mode = document.querySelector('input[name="histMode"]:checked').value;
    const histInput = document.getElementById("histFolderInput");
    const targetFolder = histInput.dataset.cleanPath || histInput.value.trim();
    const payload = { mode, target_folder: targetFolder, operation: "export" };

    if (mode === "months") {
      const years = Array.from(document.querySelectorAll('input[name="histYear"]:checked')).map(cb => parseInt(cb.value));
      const months = Array.from(document.querySelectorAll('input[name="histMonth"]:checked')).map(cb => cb.value);
      if (years.length === 0 || months.length === 0) {
        alert("Please select at least one year and one month.");
        return;
      }
      payload.years = years;
      payload.months = months;
    } else if (mode === "range") {
      const start = document.getElementById("rangeStartDate").value;
      const end = document.getElementById("rangeEndDate").value;
      if (!start || !end) {
        alert("Please select both starting date and ending date.");
        return;
      }
      payload.start_date = start;
      payload.end_date = end;
    } else if (mode === "single_date") {
      const single = document.getElementById("singleHistoricDate").value;
      if (!single) {
        alert("Please select a historic date.");
        return;
      }
      payload.target_date = single;
    }

    await triggerExecution(payload);
  });

  clearLogsBtn.addEventListener("click", () => {
    document.getElementById("liveConsole").innerHTML = "";
  });
}

async function triggerExecution(payload) {
  // Clear previous result banner
  const banner = document.getElementById("executionResultBanner");
  banner.className = "hidden p-4 rounded-xl border text-xs";
  banner.innerHTML = "";

  appendConsoleLog(`[System] Initializing ${payload.mode} extraction request...`);

  try {
    const res = await fetch("/api/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (!res.ok) {
      if (res.status === 403 && data.detail && data.detail.violations) {
        alert("OPA Security Denial:\n\n" + data.detail.violations.join("\n"));
      } else {
        alert("Execution Error: " + (data.detail || "Workflow could not be launched."));
      }
      appendConsoleLog(`[Error] Request failed: ${data.detail || res.statusText}`);
      return;
    }

    // Reflect destination folder on diagram
    const diagDestText = document.getElementById("diagDestFolderText");
    diagDestText.textContent = payload.target_folder || "Validated_Docs/old";
    diagDestText.title = payload.target_folder || "Validated_Docs/old";

    appendConsoleLog(`[System] ${data.message || "Extraction started successfully."}`);
    pollJobStatus();
  } catch (err) {
    alert("Network request error: " + err.message);
    appendConsoleLog(`[Network Error] ${err.message}`);
  }
}

// ================= 6. LIVE STATUS, DIAGRAM ANIMATION & RESULTS =================
function startStatusPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollJobStatus();
  pollInterval = setInterval(pollJobStatus, 2500);
}

async function pollJobStatus() {
  if (!authToken) return;

  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;

    const job = await res.json();
    renderJobState(job);
  } catch (err) {
    console.warn("Polling error:", err);
  }
}

function renderJobState(job) {
  const queueBadge = document.getElementById("queueBadge");
  const durationBadge = document.getElementById("jobDurationBadge");
  const progressBar = document.getElementById("jobProgressBar");
  const livePulse = document.getElementById("livePulseIndicator");

  if (!job || job.status === "idle") {
    queueBadge.textContent = "Queue: Idle";
    durationBadge.textContent = "Status: Idle";
    progressBar.style.width = "0%";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
    resetDiagramNodes();
    return;
  }

  const isRunning = (job.status === "running");
  queueBadge.textContent = `Queue: ${job.status.toUpperCase()}`;
  
  if (job.status === "completed") {
    durationBadge.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400 mr-1"></i> Run #${job.run_number || job.id || 1} Completed &bull; 100%`;
    progressBar.style.width = "100%";
    progressBar.className = "h-full bg-emerald-500 rounded-full transition-all duration-500";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
  } else if (job.status === "failed") {
    durationBadge.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400 mr-1"></i> Run #${job.run_number || job.id || 1} Failed`;
    progressBar.style.width = "100%";
    progressBar.className = "h-full bg-rose-500 rounded-full transition-all duration-500";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
  } else {
    durationBadge.textContent = `Run #${job.run_number || job.id || 1} (${job.status.toUpperCase()}) • ${job.progress_percent || 0}%`;
    progressBar.style.width = `${job.progress_percent || 0}%`;
    progressBar.className = "h-full bg-brand-500 rounded-full transition-all duration-500 animate-pulse";
    if (isRunning) {
      livePulse.classList.remove("hidden");
      livePulse.classList.add("inline-flex");
    } else {
      livePulse.classList.add("hidden");
      livePulse.classList.remove("inline-flex");
    }
  }

  // Update Diagram Nodes and Animated Arrows
  updateDiagramColors(job);

  // Update 4 Core Table Status Badges
  updateTableBadges(job.tables_status);

  // Update Logs Console
  if (job.logs && job.logs.length > 0) {
    job.logs.forEach(l => appendConsoleLog(l));
  }

  // Update Final Result Banner
  renderResultBanner(job);
}

function updateTableBadges(tablesStatus) {
  if (!tablesStatus) return;

  const mapping = [
    { key: "Revenue in Depth", id: "tblBadge_revenue" },
    { key: "Access in Depth", id: "tblBadge_access" },
    { key: "System Event in Depth", id: "tblBadge_system" },
    { key: "Parking Transactions", id: "tblBadge_parking" },
  ];

  mapping.forEach(({ key, id }) => {
    const el = document.getElementById(id);
    if (!el) return;
    const chip = el.querySelector(".status-chip");
    const st = tablesStatus[key] || "pending";

    if (st === "downloading") {
      chip.textContent = "Downloading...";
      chip.className = "status-chip text-[10px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse";
    } else if (st === "done") {
      chip.innerHTML = '<i class="fa-solid fa-check mr-1"></i> Fetched';
      chip.className = "status-chip text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-semibold";
    } else {
      chip.textContent = "Pending";
      chip.className = "status-chip text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-400";
    }
  });
}

function updateDiagramColors(job) {
  const nodeSkidata = document.getElementById("node_skidata");
  const nodeScraper = document.getElementById("node_scraper");
  const nodeTables = document.getElementById("node_tables");
  const nodeDest = document.getElementById("node_destination");
  const arr1 = document.getElementById("arrow_1");
  const arr2 = document.getElementById("arrow_2");
  const arr3 = document.getElementById("arrow_3");

  resetDiagramNodes();

  const setNode = (el, pillText, state) => {
    el.className = `diagram-node flex-1 p-4 rounded-xl state-${state} text-center shadow-md`;
    el.querySelector(".node-pill").textContent = pillText;
  };

  if (job.status === "failed") {
    setNode(nodeScraper, "Failed", "failed");
    setNode(nodeDest, "Aborted", "failed");
    return;
  }

  // When job is completed, ALL nodes and arrows are immediately set to completed!
  if (job.status === "completed") {
    setNode(nodeSkidata, "Connected", "completed");
    arr1.className = "diagram-arrow arrow-done text-xl px-1";

    setNode(nodeScraper, "Completed", "completed");
    arr2.className = "diagram-arrow arrow-done text-xl px-1";

    setNode(nodeTables, "All 4 Tables Ready", "completed");
    arr3.className = "diagram-arrow arrow-done text-xl px-1";

    setNode(nodeDest, "Verified & Saved", "completed");
    return;
  }

  const active = job.active_node || "";
  const completed = job.completed_nodes || [];

  // Node 1: SkiData Hub
  if (active === "skidata_auth") {
    setNode(nodeSkidata, "Authenticating...", "running");
    arr1.className = "diagram-arrow arrow-active text-xl px-1";
  } else if (completed.includes("skidata_auth") || active) {
    setNode(nodeSkidata, "Connected", "completed");
    arr1.className = "diagram-arrow arrow-done text-xl px-1";
  }

  // Node 2: Scraper Engine
  if (active === "scraper_engine") {
    setNode(nodeScraper, "Scraping Active", "running");
    arr2.className = "diagram-arrow arrow-active text-xl px-1";
  } else if (completed.includes("scraper_engine") || active.startsWith("table_") || active === "destination_upload") {
    setNode(nodeScraper, "Completed", "completed");
    arr2.className = "diagram-arrow arrow-done text-xl px-1";
  }

  // Node 3: Tables Ingestion
  if (active.startsWith("table_")) {
    let lbl = "Fetching Tables";
    if (active === "table_revenue") lbl = "Table 1/4 (Revenue)";
    else if (active === "table_access") lbl = "Table 2/4 (Access)";
    else if (active === "table_system") lbl = "Table 3/4 (System Event)";
    else if (active === "table_parking") lbl = "Table 4/4 (Parking)";

    setNode(nodeTables, lbl, "running");
    arr2.className = "diagram-arrow arrow-done text-xl px-1";
    arr3.className = "diagram-arrow arrow-active text-xl px-1";
  } else if (completed.includes("table_parking") || active === "destination_upload") {
    setNode(nodeTables, "All 4 Tables Ready", "completed");
    arr3.className = "diagram-arrow arrow-done text-xl px-1";
  }

  // Node 4: Destination Upload
  if (active === "destination_upload") {
    setNode(nodeDest, "Uploading...", "running");
    arr3.className = "diagram-arrow arrow-active text-xl px-1";
  }
}

function resetDiagramNodes() {
  document.querySelectorAll(".diagram-node").forEach(n => {
    n.className = "diagram-node flex-1 p-4 rounded-xl state-idle text-center shadow-md";
    n.querySelector(".node-pill").textContent = "Idle";
  });
  document.querySelectorAll(".diagram-arrow").forEach(a => {
    a.className = "diagram-arrow text-slate-600 text-xl px-1";
  });
}

function renderResultBanner(job) {
  const banner = document.getElementById("executionResultBanner");
  if (!banner) return;

  if (job.status === "completed") {
    banner.classList.remove("hidden");
    const targetFolder = document.getElementById("diagDestFolderText").textContent || "Validated_Docs/old";
    banner.className = "p-4 rounded-xl border text-xs bg-emerald-500/15 border-emerald-500/30 text-emerald-200 space-y-1";
    banner.innerHTML = `
      <div class="flex items-center gap-2 font-bold text-emerald-300 text-sm">
        <i class="fa-solid fa-circle-check"></i>
        <span>Extraction Finished Successfully!</span>
      </div>
      <p>All 4 core tables fetched: <strong>Revenue in Depth, Access in Depth, System Event in Depth, Parking Transactions</strong>.</p>
      <p class="text-[11px] text-emerald-300/80">Delivered & verified in SharePoint folder: <strong>${escapeHtml(targetFolder)}</strong>.</p>
    `;
  } else if (job.status === "failed") {
    banner.classList.remove("hidden");
    banner.className = "p-4 rounded-xl border text-xs bg-rose-500/15 border-rose-500/30 text-rose-200 space-y-1";
    banner.innerHTML = `
      <div class="flex items-center gap-2 font-bold text-rose-300 text-sm">
        <i class="fa-solid fa-circle-xmark"></i>
        <span>Workflow Notice</span>
      </div>
      <p class="font-mono">${escapeHtml(job.error || "Extraction did not complete or requested month was not found in SkiData.")}</p>
    `;
  } else {
    banner.classList.add("hidden");
  }
}

let loggedMessages = new Set();
function appendConsoleLog(msg) {
  if (loggedMessages.has(msg)) return;
  loggedMessages.add(msg);

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
