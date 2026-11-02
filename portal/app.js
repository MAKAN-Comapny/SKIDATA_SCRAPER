/**
 * SKIDATA Data Operations Portal - Frontend Application Controller
 * Handles authentication, folder verification, live diagram states, and execution monitoring.
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
        const data = await res.json();
        throw new Error(data.detail || "Authentication failed");
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
    if (dailyFolder) diagDestText.textContent = dailyFolder;
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
    if (histFolder) diagDestText.textContent = histFolder;
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
    folderCheckDebounce = setTimeout(verifyDailyFolder, 400);
  });
  verifyDailyBtn.addEventListener("click", verifyDailyFolder);

  histInput.addEventListener("input", () => {
    clearTimeout(folderCheckDebounce);
    folderCheckDebounce = setTimeout(verifyHistFolder, 400);
  });
  verifyHistBtn.addEventListener("click", verifyHistFolder);

  // Check on load
  verifyDailyFolder();
  verifyHistFolder();
}

async function verifyDailyFolder() {
  const input = document.getElementById("dailyFolderInput");
  const statusDiv = document.getElementById("dailyFolderStatus");
  const statusText = document.getElementById("dailyFolderStatusText");
  const spinner = document.getElementById("dailyFolderSpinner");
  const runBtn = document.getElementById("runDailyBtn");
  const diagDestText = document.getElementById("diagDestFolderText");

  const val = input.value.trim();
  if (!val) {
    statusText.textContent = "no accessible (folder cannot be empty)";
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
    return;
  }

  spinner.classList.remove("hidden");
  statusText.textContent = "Verifying folder accessibility...";

  try {
    const res = await fetch("/api/verify-folder", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${authToken}`
      },
      body: JSON.stringify({ folder_input: val })
    });

    const data = await res.json();
    spinner.classList.add("hidden");

    if (data.accessible) {
      statusText.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400"></i> ${data.message || 'Folder verified & accessible'}`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-emerald-400";
      runBtn.disabled = false;
      if (activeTab === "daily") {
        diagDestText.textContent = data.path || val;
        diagDestText.title = data.path || val;
      }
    } else {
      // User requirement: "if not write a message below 'no accessible'"
      const err = data.error || 'Access Denied';
      const msg = err.startsWith('no accessible') ? err : `no accessible: ${err}`;
      statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> ${msg}`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
      runBtn.disabled = true;
    }
  } catch (err) {
    spinner.classList.add("hidden");
    statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> no accessible`;
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
  }
}

async function verifyHistFolder() {
  const input = document.getElementById("histFolderInput");
  const statusDiv = document.getElementById("histFolderStatus");
  const statusText = document.getElementById("histFolderStatusText");
  const spinner = document.getElementById("histFolderSpinner");
  const runBtn = document.getElementById("runHistBtn");
  const diagDestText = document.getElementById("diagDestFolderText");

  const val = input.value.trim();
  if (!val) {
    statusText.textContent = "no accessible (folder cannot be empty)";
    statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
    runBtn.disabled = true;
    return;
  }

  spinner.classList.remove("hidden");
  statusText.textContent = "Verifying folder accessibility...";

  try {
    const res = await fetch("/api/verify-folder", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${authToken}`
      },
      body: JSON.stringify({ folder_input: val })
    });

    const data = await res.json();
    spinner.classList.add("hidden");

    if (data.accessible) {
      statusText.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400"></i> Folder accessible (will write to: ${data.path})`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-emerald-400";
      runBtn.disabled = false;
      if (activeTab === "hist") {
        diagDestText.textContent = data.path || val;
        diagDestText.title = data.path || val;
      }
    } else {
      // User requirement: "if not write a message below 'no accessible'"
      const err = data.error || 'Access Denied';
      const msg = err.startsWith('no accessible') ? err : `no accessible: ${err}`;
      statusText.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400"></i> ${msg}`;
      statusDiv.className = "mt-2 text-xs flex items-center gap-1.5 font-medium text-rose-400";
      runBtn.disabled = true;
    }
  } catch (err) {
    spinner.classList.add("hidden");
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
    const targetFolder = document.getElementById("dailyFolderInput").value.trim();
    await triggerExecution({
      mode: "daily",
      target_folder: targetFolder
    });
  });

  runHistBtn.addEventListener("click", async () => {
    const mode = document.querySelector('input[name="histMode"]:checked').value;
    const targetFolder = document.getElementById("histFolderInput").value.trim();
    const payload = { mode, target_folder: targetFolder };

    if (mode === "months") {
      const years = Array.from(document.querySelectorAll('input[name="histYear"]:checked')).map(cb => parseInt(cb.value));
      const months = Array.from(document.querySelectorAll('input[name="histMonth"]:checked')).map(cb => cb.value);
      if (years.length === 0 || months.length === 0) {
        alert("Please select at least one year (e.g. 2025/2026) and at least one month.");
        return;
      }
      payload.years = years;
      payload.months = months;
    } else if (mode === "range") {
      const start = document.getElementById("rangeStartDate").value;
      const end = document.getElementById("rangeEndDate").value;
      if (!start || !end) {
        alert("Please pick both the Starting Date and Ending Date from the calendar.");
        return;
      }
      payload.start_date = start;
      payload.end_date = end;
    } else if (mode === "single_date") {
      const single = document.getElementById("singleHistoricDate").value;
      if (!single) {
        alert("Please pick the Historic Day from the calendar.");
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

  try {
    const res = await fetch("/api/execute", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${authToken}`
      },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (!res.ok) {
      if (res.status === 403) {
        const violations = data.detail.violations || [data.detail.error];
        alert("OPA Security Denial:\n\n" + violations.join("\n"));
      } else {
        alert("Execution Error: " + (data.detail || "Workflow failed to start."));
      }
      return;
    }

    // Reflect destination folder on the diagram immediately
    const diagDestText = document.getElementById("diagDestFolderText");
    diagDestText.textContent = payload.target_folder;
    diagDestText.title = payload.target_folder;

    pollJobStatus();
  } catch (err) {
    alert("Network request error: " + err.message);
  }
}

// ================= 6. LIVE STATUS, DIAGRAM ANIMATION & RESULTS =================
function startStatusPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollJobStatus();
  pollInterval = setInterval(pollJobStatus, 2000);
}

async function pollJobStatus() {
  if (!authToken) return;

  try {
    const res = await fetch("/api/job/status", {
      headers: { "Authorization": `Bearer ${authToken}` }
    });
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
  const liveConsole = document.getElementById("liveConsole");
  const livePulse = document.getElementById("livePulseIndicator");
  const histProgressCard = document.getElementById("histBatchProgressCard");

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
    durationBadge.innerHTML = `<i class="fa-solid fa-circle-check text-emerald-400 mr-1"></i> Job #${job.id || 1} Completed &bull; 100%`;
    progressBar.style.width = "100%";
    progressBar.className = "h-full bg-emerald-500 rounded-full transition-all duration-500";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
  } else if (job.status === "failed") {
    durationBadge.innerHTML = `<i class="fa-solid fa-circle-xmark text-rose-400 mr-1"></i> Job #${job.id || 1} Failed`;
    progressBar.style.width = "100%";
    progressBar.className = "h-full bg-rose-500 rounded-full transition-all duration-500";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
  } else {
    durationBadge.textContent = `Job #${job.id} (${job.status.toUpperCase()}) • ${job.progress_percent || 0}%`;
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
    const formattedLogs = job.logs.map(l => `<p class="leading-relaxed font-mono">${escapeHtml(l)}</p>`).join("");
    liveConsole.innerHTML = formattedLogs;
    liveConsole.scrollTop = liveConsole.scrollHeight;
  }

  // Update Historical Batch Matrix if applicable
  if (job.payload && job.payload.mode === "months") {
    histProgressCard.classList.remove("hidden");
    renderBatchMatrix(job);
  } else {
    histProgressCard.classList.add("hidden");
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

function renderBatchMatrix(job) {
  const badgeGrid = document.getElementById("batchesBadgeGrid");
  const counter = document.getElementById("histBatchCounter");
  const years = job.payload.years || [2026];
  const months = job.payload.months || [];

  let badges = "";
  let doneCount = 0;
  const total = years.length * months.length;

  years.forEach(y => {
    months.forEach(m => {
      const name = `${m.substring(0, 3)} ${y}`;
      let statusClass = "bg-slate-800 text-slate-400 border-slate-700";
      let statusIcon = '<i class="fa-solid fa-clock"></i>';
      let statusText = "Queued";

      if (job.result && job.result.results) {
        const found = job.result.results.find(r => r.batch === `${m} ${y}`);
        if (found) {
          if (found.success) {
            statusClass = "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
            statusIcon = '<i class="fa-solid fa-check"></i>';
            statusText = "Completed";
            doneCount++;
          } else {
            statusClass = "bg-rose-500/20 text-rose-300 border-rose-500/40";
            statusIcon = '<i class="fa-solid fa-triangle-exclamation"></i>';
            statusText = "Not in SkiData / Error";
          }
        }
      } else if (job.status === "running") {
        statusClass = "bg-sky-500/20 text-sky-300 border-sky-500/40 animate-pulse";
        statusIcon = '<i class="fa-solid fa-spinner fa-spin"></i>';
        statusText = "Processing";
      }

      badges += `
        <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl border text-xs font-medium ${statusClass}">
          ${statusIcon}
          <span>${name}</span>
          <span class="text-[10px] opacity-75">(${statusText})</span>
        </span>
      `;
    });
  });

  badgeGrid.innerHTML = badges;
  counter.textContent = `${doneCount} / ${total} Completed`;
}

function renderResultBanner(job) {
  const banner = document.getElementById("executionResultBanner");
  if (!banner) return;

  if (job.status === "completed") {
    banner.classList.remove("hidden");
    const targetFolder = job.payload ? job.payload.target_folder : "SharePoint";

    // Check if any month failed or had not present warning
    let hasWarnings = false;
    let warningNotes = [];
    if (job.result && job.result.results) {
      job.result.results.forEach(r => {
        if (!r.success) {
          hasWarnings = true;
          warningNotes.push(`${r.batch}: Not present in SkiData or extraction failed`);
        }
      });
    }

    if (hasWarnings) {
      banner.className = "p-4 rounded-xl border text-xs bg-amber-500/15 border-amber-500/30 text-amber-200 space-y-1.5";
      banner.innerHTML = `
        <div class="flex items-center gap-2 font-bold text-amber-300 text-sm">
          <i class="fa-solid fa-triangle-exclamation"></i>
          <span>Historical Run Completed with Notices</span>
        </div>
        <p>Some requested months could not be found in SkiData analytics portal:</p>
        <ul class="list-disc list-inside space-y-0.5 text-amber-300 font-mono">
          ${warningNotes.map(n => `<li>${escapeHtml(n)}</li>`).join("")}
        </ul>
        <p class="text-[11px] text-amber-400/80">Available sheets and tables were successfully delivered to <strong>${escapeHtml(targetFolder)}</strong>.</p>
      `;
    } else {
      banner.className = "p-4 rounded-xl border text-xs bg-emerald-500/15 border-emerald-500/30 text-emerald-200 space-y-1";
      banner.innerHTML = `
        <div class="flex items-center gap-2 font-bold text-emerald-300 text-sm">
          <i class="fa-solid fa-circle-check"></i>
          <span>Workflow Completed Successfully!</span>
        </div>
        <p>All core tables fetched: <strong>Revenue in Depth, Access in Depth, System Event in Depth, Parking Transactions</strong>.</p>
        <p class="text-[11px] text-emerald-300/80">Delivered & verified in SharePoint folder: <strong>${escapeHtml(targetFolder)}</strong>.</p>
      `;
    }
  } else if (job.status === "failed") {
    banner.classList.remove("hidden");
    banner.className = "p-4 rounded-xl border text-xs bg-rose-500/15 border-rose-500/30 text-rose-200 space-y-1";
    banner.innerHTML = `
      <div class="flex items-center gap-2 font-bold text-rose-300 text-sm">
        <i class="fa-solid fa-circle-xmark"></i>
        <span>Workflow Failed</span>
      </div>
      <p class="font-mono">${escapeHtml(job.error || "An unexpected error occurred during execution.")}</p>
    `;
  } else {
    banner.classList.add("hidden");
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
