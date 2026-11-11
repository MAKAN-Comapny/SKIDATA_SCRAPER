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
let isDispatching = false;
let activeJobStatus = "idle";
let lastDispatchedAt = 0;
let pendingModalCallback = null;
let validationEnabled = sessionStorage.getItem("skidata_validation_enabled") !== "false";
let latestValidationReport = null;
let latestInsightsJob = null;
let insightActiveView = "daily";
let insightCpMetric = "financial";
let insightLastReportKey = null;
let insightLastDrawSignature = null;
const insightCharts = { time: null, mix: null, carPark: null };

document.addEventListener("DOMContentLoaded", () => {
  initAuth();
  initValidationToggle();
  initValidationReportDrawer();
  initInsightsControls();
  initTabs();
  initMonthsGrid();
  initHistModeSwitch();
  initFolderVerification();
  initFileStructureModal();
  initJobActions();
});

// ================= 0. VALIDATION GATE SWITCH & AUDIT DRAWER =================
function initValidationToggle() {
  const toggleBtn = document.getElementById("validationGateToggle");
  updateValidationToggleUI();
  if (!toggleBtn) return;

  toggleBtn.addEventListener("click", () => {
    validationEnabled = !validationEnabled;
    sessionStorage.setItem("skidata_validation_enabled", validationEnabled ? "true" : "false");
    updateValidationToggleUI();
    appendConsoleLog(
      `[Validation Gate] Switched ${validationEnabled ? "ACTIVE (strict schema, calendar & financial checks ON)" : "OFF (fast bypass mode for time-constrained runs)"}.`
    );
  });
}

function updateValidationToggleUI() {
  const toggleBtn = document.getElementById("validationGateToggle");
  const dot = document.getElementById("validationDot");
  const label = document.getElementById("validationLabel");
  const track = document.getElementById("validationSwitchTrack");
  const thumb = document.getElementById("validationSwitchThumb");
  const runHint = document.getElementById("validationRunHint");

  if (toggleBtn) {
    toggleBtn.setAttribute("aria-checked", validationEnabled ? "true" : "false");
    toggleBtn.className = validationEnabled
      ? "flex items-center gap-2 text-xs bg-emerald-950/60 hover:bg-emerald-900/60 border border-emerald-500/40 px-3 py-1.5 rounded-full transition-all duration-200 cursor-pointer"
      : "flex items-center gap-2 text-xs bg-amber-950/60 hover:bg-amber-900/60 border border-amber-500/50 px-3 py-1.5 rounded-full transition-all duration-200 cursor-pointer";
  }
  if (dot) {
    dot.className = validationEnabled
      ? "w-2 h-2 rounded-full bg-emerald-400 animate-pulse"
      : "w-2 h-2 rounded-full bg-amber-400";
  }
  if (label) {
    label.textContent = validationEnabled ? "Validation Gate: Active" : "Validation Gate: Off";
    label.className = validationEnabled ? "text-emerald-200 font-medium" : "text-amber-200 font-medium";
  }
  if (track && thumb) {
    track.className = validationEnabled
      ? "ml-1 inline-flex items-center w-8 h-4 rounded-full bg-emerald-500/80 p-0.5 transition-colors"
      : "ml-1 inline-flex items-center w-8 h-4 rounded-full bg-slate-600 p-0.5 transition-colors";
    thumb.className = validationEnabled
      ? "w-3 h-3 rounded-full bg-white shadow transform translate-x-4 transition-transform"
      : "w-3 h-3 rounded-full bg-slate-200 shadow transform translate-x-0 transition-transform";
  }
  if (runHint) {
    runHint.className = validationEnabled
      ? "text-[11px] text-emerald-400 font-medium flex items-center gap-1.5"
      : "text-[11px] text-amber-400 font-medium flex items-center gap-1.5";
    runHint.innerHTML = validationEnabled
      ? '<i class="fa-solid fa-shield-halved"></i><span>Validation Gate Active: Calendar completeness, XML cell types, revenue balance &amp; anomalies verified before upload.</span>'
      : '<i class="fa-solid fa-bolt"></i><span>Validation Gate OFF (Time-Constraint Bypass): Direct upload mode — data quality &amp; financial checks skipped.</span>';
  }

  if (activeJobStatus === "idle") {
    const nodeVal = document.getElementById("node_validation");
    if (nodeVal) {
      nodeVal.className = validationEnabled
        ? "diagram-node flex-1 p-4 rounded-xl state-idle text-center shadow-md"
        : "diagram-node flex-1 p-4 rounded-xl state-bypassed text-center shadow-md";
      const pill = nodeVal.querySelector(".node-pill");
      if (pill) pill.textContent = validationEnabled ? "Armed" : "Off";
    }
    renderValidationReport(null);
  }
}

let valJsonLastReportKey = null;

function updateValJsonToggleLabel(isHidden) {
  const toggleBtn = document.getElementById("valToggleJsonBtn");
  if (!toggleBtn) return;
  toggleBtn.innerHTML = isHidden
    ? '<i class="fa-solid fa-code mr-1 text-brand-400"></i>Show JSON'
    : '<i class="fa-solid fa-code mr-1 text-brand-400"></i>Hide JSON';
}

function initValidationReportDrawer() {
  const toggleBtn = document.getElementById("valToggleJsonBtn");
  const copyBtn = document.getElementById("valCopyJsonBtn");
  const downloadBtn = document.getElementById("valDownloadJsonBtn");
  const wrapper = document.getElementById("valJsonWrapper");

  if (toggleBtn && wrapper) {
    toggleBtn.addEventListener("click", () => {
      wrapper.classList.toggle("hidden");
      updateValJsonToggleLabel(wrapper.classList.contains("hidden"));
    });
  }
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      if (!latestValidationReport) return;
      try {
        await navigator.clipboard.writeText(JSON.stringify(latestValidationReport, null, 2));
        copyBtn.innerHTML = '<i class="fa-solid fa-check text-emerald-400 mr-1"></i>Copied';
        setTimeout(() => {
          copyBtn.innerHTML = '<i class="fa-regular fa-copy mr-1"></i>Copy JSON';
        }, 1600);
      } catch {
        // Fallback silently if clipboard permission unavailable
      }
    });
  }
  if (downloadBtn) {
    downloadBtn.addEventListener("click", () => {
      if (!latestValidationReport) return;
      const prefix = String(latestValidationReport.file_prefix || "skidata").replace(/[^a-zA-Z0-9_-]/g, "_");
      const blob = new Blob([JSON.stringify(latestValidationReport, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${prefix}_validation_report.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
}

function initInsightsControls() {
  document.querySelectorAll(".insight-view-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      insightActiveView = btn.dataset.insightView || "daily";
      insightLastDrawSignature = null;
      if (latestInsightsJob) renderDataInsights(latestInsightsJob);
    });
  });
  document.querySelectorAll(".insight-cp-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      insightCpMetric = btn.dataset.cpMetric || "financial";
      insightLastDrawSignature = null;
      if (latestInsightsJob) renderDataInsights(latestInsightsJob);
    });
  });
}

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
      if (!isWorkflowBusy()) {
        runBtn.disabled = false;
      }
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
      if (!isWorkflowBusy()) {
        runBtn.disabled = false;
      }
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

// ================= 5. FILE STRUCTURE MODAL & HELPERS =================
function isWorkflowBusy() {
  return isDispatching || activeJobStatus === "running" || activeJobStatus === "queued";
}

function setWorkflowButtonsBusy(mode = "daily", label = "Initializing Run...") {
  const runDailyBtn = document.getElementById("runDailyBtn");
  const runHistBtn = document.getElementById("runHistBtn");

  if (runDailyBtn) {
    runDailyBtn.disabled = true;
    runDailyBtn.classList.add("cursor-not-allowed");
    if (mode === "daily") {
      runDailyBtn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin text-xs mr-2"></i><span>${label}</span>`;
      runDailyBtn.classList.remove("opacity-50");
      runDailyBtn.classList.add("opacity-75");
    } else {
      runDailyBtn.innerHTML = `<i class="fa-solid fa-lock text-xs mr-2 opacity-60"></i><span>Pipeline Locked</span>`;
      runDailyBtn.classList.add("opacity-50");
    }
  }

  if (runHistBtn) {
    runHistBtn.disabled = true;
    runHistBtn.classList.add("cursor-not-allowed");
    if (mode === "historical" || mode === "hist") {
      runHistBtn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin text-xs mr-2"></i><span>${label}</span>`;
      runHistBtn.classList.remove("opacity-50");
      runHistBtn.classList.add("opacity-75");
    } else {
      runHistBtn.innerHTML = `<i class="fa-solid fa-lock text-xs mr-2 opacity-60"></i><span>Pipeline Locked</span>`;
      runHistBtn.classList.add("opacity-50");
    }
  }
}

function restoreWorkflowButtons() {
  if (isWorkflowBusy()) return;

  const runDailyBtn = document.getElementById("runDailyBtn");
  const runHistBtn = document.getElementById("runHistBtn");
  const dailyInput = document.getElementById("dailyFolderInput");
  const histInput = document.getElementById("histFolderInput");

  if (runDailyBtn) {
    runDailyBtn.innerHTML = `<i class="fa-solid fa-play text-xs mr-2"></i><span>Run Daily Ingestion Workflow</span>`;
    runDailyBtn.classList.remove("opacity-75", "opacity-60", "opacity-50", "cursor-not-allowed");
    runDailyBtn.disabled = !Boolean(dailyInput && dailyInput.dataset.cleanPath);
  }

  if (runHistBtn) {
    runHistBtn.innerHTML = `<i class="fa-solid fa-forward text-xs mr-2"></i><span>Start Historical Extraction</span>`;
    runHistBtn.classList.remove("opacity-75", "opacity-60", "opacity-50", "cursor-not-allowed");
    runHistBtn.disabled = !Boolean(histInput && histInput.dataset.cleanPath);
  }
}

function getDaysDifference(startStr, endStr) {
  if (!startStr || !endStr) return 0;
  const [y1, m1, d1] = startStr.split("-").map(Number);
  const [y2, m2, d2] = endStr.split("-").map(Number);
  const dt1 = new Date(y1, m1 - 1, d1);
  const dt2 = new Date(y2, m2 - 1, d2);
  const diffTime = dt2.getTime() - dt1.getTime();
  return Math.max(1, Math.round(diffTime / (1000 * 60 * 60 * 24)) + 1);
}

function initFileStructureModal() {
  const modal = document.getElementById("fileStructureModal");
  const closeBtn = document.getElementById("closeFileStructureModalBtn");
  const cancelBtn = document.getElementById("cancelFileStructureBtn");
  const confirmBtn = document.getElementById("confirmFileStructureBtn");

  if (!modal) return;

  function closeModal() {
    modal.classList.add("hidden");
    pendingModalCallback = null;
  }

  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  if (cancelBtn) cancelBtn.addEventListener("click", closeModal);

  // Close on backdrop click
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  // Close on Escape key
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) {
      closeModal();
    }
  });

  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      const selected = document.querySelector('input[name="fileStructureChoice"]:checked');
      const choice = selected ? selected.value : "separate";
      const cb = pendingModalCallback;
      closeModal();
      if (typeof cb === "function") {
        cb(choice);
      }
    });
  }
}

function promptFileStructureModal(config, onConfirm) {
  const modal = document.getElementById("fileStructureModal");
  const titleEl = document.getElementById("fileStructureModalTitle");
  const subtitleEl = document.getElementById("fileStructureModalSubtitle");
  const optionsContainer = document.getElementById("fileStructureOptions");

  if (!modal || !optionsContainer) {
    onConfirm(config.defaultChoice || "separate");
    return;
  }

  pendingModalCallback = onConfirm;

  titleEl.textContent = config.title || "Select File Structure";
  subtitleEl.textContent = config.subtitle || "Choose how extracted tables will be organized in SharePoint";

  let options = [];
  if (config.type === "multiday") {
    options = [
      {
        value: "separate",
        title: "Day-wise Separate Files",
        badge: "Recommended",
        icon: '<i class="fa-solid fa-calendar-day text-brand-400"></i>',
        desc: "Maintains distinct individual daily files for each date. Best for daily archival and incremental processing."
      },
      {
        value: "merged",
        title: "Single Consolidated Merge",
        badge: "Consolidated",
        icon: '<i class="fa-solid fa-file-excel text-sky-400"></i>',
        desc: "Combines all extracted dates into 1 consolidated master file per table. Best for single-table bulk analysis."
      }
    ];
  } else {
    // multimonth
    options = [
      {
        value: "separate",
        title: "All Days Separate (Day-wise)",
        badge: "Granular",
        icon: '<i class="fa-solid fa-calendar-days text-brand-400"></i>',
        desc: "Generates separate individual daily Excel files for each day across all selected months."
      },
      {
        value: "monthly",
        title: "Monthly Aggregated (1 File per Month)",
        badge: "Recommended",
        icon: '<i class="fa-solid fa-calendar text-emerald-400"></i>',
        desc: "Aggregates records into one consolidated file per month (e.g. August file, September file)."
      },
      {
        value: "merged",
        title: "Complete Master Merge",
        badge: "Single Master",
        icon: '<i class="fa-solid fa-layer-group text-sky-400"></i>',
        desc: "Consolidates all months and all days into a single comprehensive master workbook per table."
      }
    ];
  }

  const defaultVal = config.defaultChoice || (config.type === "multiday" ? "separate" : "monthly");

  optionsContainer.innerHTML = options.map((opt, i) => {
    const isChecked = (opt.value === defaultVal) || (i === 0 && !defaultVal);
    return `
      <label class="structure-card cursor-pointer p-4 rounded-xl border border-slate-700/80 bg-slate-800/60 hover:bg-slate-800 hover:border-brand-500/60 transition flex items-start gap-3.5 select-none">
        <input type="radio" name="fileStructureChoice" value="${opt.value}" ${isChecked ? 'checked' : ''} class="mt-1 text-brand-500 focus:ring-brand-500">
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <div class="w-6 h-6 rounded-lg bg-slate-700/60 flex items-center justify-center text-xs">
              ${opt.icon}
            </div>
            <span class="text-xs font-bold text-white">${opt.title}</span>
            ${opt.badge ? `<span class="text-[10px] px-2 py-0.5 rounded-full font-semibold ${opt.badge === 'Recommended' ? 'bg-brand-500/20 text-brand-300 border border-brand-500/30' : 'bg-slate-700 text-slate-300'}">${opt.badge}</span>` : ''}
          </div>
          <p class="text-[11px] text-slate-400 mt-1.5 leading-relaxed">${opt.desc}</p>
        </div>
      </label>
    `;
  }).join("");

  modal.classList.remove("hidden");
}

// ================= 6. JOB EXECUTION =================
function initJobActions() {
  const runDailyBtn = document.getElementById("runDailyBtn");
  const runHistBtn = document.getElementById("runHistBtn");
  const clearLogsBtn = document.getElementById("clearLogsBtn");

  runDailyBtn.addEventListener("click", () => {
    if (isWorkflowBusy()) return;
    const dailyInput = document.getElementById("dailyFolderInput");
    const targetFolder = dailyInput.dataset.cleanPath || dailyInput.value.trim();

    // Daily workflow extracts rolling 5 days (> 1 day, <= 31 days)
    promptFileStructureModal({
      type: "multiday",
      title: "Daily Rolling Ingestion (5 Days)",
      subtitle: "The daily workflow captures 5 rolling days. Choose your output organization in SharePoint:",
      defaultChoice: "separate"
    }, async (mergeChoice) => {
      await triggerExecution({
        mode: "daily",
        target_folder: targetFolder,
        merge_option: mergeChoice
      });
    });
  });

  runHistBtn.addEventListener("click", () => {
    if (isWorkflowBusy()) return;
    const mode = document.querySelector('input[name="histMode"]:checked').value;
    const histInput = document.getElementById("histFolderInput");
    const targetFolder = histInput.dataset.cleanPath || histInput.value.trim();
    const payload = { mode, target_folder: targetFolder, operation: "export" };

    if (mode === "single_date") {
      const single = document.getElementById("singleHistoricDate").value;
      if (!single) {
        alert("Please select a historic date.");
        return;
      }
      payload.target_date = single;
      // Exactly 1 day: bypass modal and run directly
      payload.merge_option = "separate";
      triggerExecution(payload);
      return;
    }

    if (mode === "range") {
      const start = document.getElementById("rangeStartDate").value;
      const end = document.getElementById("rangeEndDate").value;
      if (!start || !end) {
        alert("Please select both starting date and ending date.");
        return;
      }
      if (start > end) {
        alert("Starting date cannot be after ending date.");
        return;
      }
      payload.start_date = start;
      payload.end_date = end;

      const diffDays = getDaysDifference(start, end);
      if (diffDays <= 1) {
        // Exactly 1 day: bypass modal and run directly
        payload.merge_option = "separate";
        triggerExecution(payload);
        return;
      }

      if (diffDays <= 31) {
        // 2 to 31 days: multi-day prompt (separate vs merged)
        promptFileStructureModal({
          type: "multiday",
          title: `Date Range Ingestion (${diffDays} Days)`,
          subtitle: `Extraction spans from ${start} to ${end} (${diffDays} days). Choose file structure:`,
          defaultChoice: "separate"
        }, (mergeChoice) => {
          payload.merge_option = mergeChoice;
          triggerExecution(payload);
        });
      } else {
        // > 31 days: multi-month prompt (separate vs monthly vs master merged)
        promptFileStructureModal({
          type: "multimonth",
          title: `Multi-Month Date Range (${diffDays} Days)`,
          subtitle: `Extraction spans multiple months (${start} to ${end}). Choose how files should be aggregated:`,
          defaultChoice: "monthly"
        }, (mergeChoice) => {
          payload.merge_option = mergeChoice;
          triggerExecution(payload);
        });
      }
      return;
    }

    if (mode === "months") {
      const years = Array.from(document.querySelectorAll('input[name="histYear"]:checked')).map(cb => parseInt(cb.value));
      const months = Array.from(document.querySelectorAll('input[name="histMonth"]:checked')).map(cb => cb.value);
      if (years.length === 0 || months.length === 0) {
        alert("Please select at least one year and one month.");
        return;
      }
      payload.years = years;
      payload.months = months;

      if (months.length === 1) {
        // Single month (<= 31 days)
        promptFileStructureModal({
          type: "multiday",
          title: `Historical Ingestion (${months[0]} ${years.join(", ")})`,
          subtitle: `Selected 1 full month (${months[0]}). Choose how tables should be organized:`,
          defaultChoice: "separate"
        }, (mergeChoice) => {
          payload.merge_option = mergeChoice;
          triggerExecution(payload);
        });
      } else {
        // Multiple months (> 1 month)
        promptFileStructureModal({
          type: "multimonth",
          title: `Multi-Month Historical Ingestion (${months.length} Months)`,
          subtitle: `Selected ${months.length} months (${months.join(", ")}). Choose aggregation structure:`,
          defaultChoice: "monthly"
        }, (mergeChoice) => {
          payload.merge_option = mergeChoice;
          triggerExecution(payload);
        });
      }
      return;
    }
  });

  clearLogsBtn.addEventListener("click", () => {
    document.getElementById("liveConsole").innerHTML = "";
  });
}

async function triggerExecution(payload) {
  if (isWorkflowBusy()) return;

  payload.validate = validationEnabled;

  // Clear previous result banner and reset validation drawer
  const banner = document.getElementById("executionResultBanner");
  banner.className = "hidden p-4 rounded-xl border text-xs";
  banner.innerHTML = "";
  renderValidationReport(null, { pending: true, enabled: validationEnabled });

  isDispatching = true;
  lastDispatchedAt = Date.now();
  setWorkflowButtonsBusy(payload.mode === "daily" ? "daily" : "historical", "Launching Job...");

  const queueBadge = document.getElementById("queueBadge");
  const durationBadge = document.getElementById("jobDurationBadge");
  const progressBar = document.getElementById("jobProgressBar");
  const livePulse = document.getElementById("livePulseIndicator");

  queueBadge.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin text-amber-400 mr-1.5"></i> DISPATCHING...`;
  durationBadge.innerHTML = `<span class="text-amber-400 font-semibold"><i class="fa-solid fa-cloud-arrow-up animate-pulse mr-1"></i> Creating Workflow Run...</span>`;
  progressBar.style.width = "10%";
  progressBar.className = "h-full bg-amber-500 rounded-full transition-all duration-500 animate-pulse";
  livePulse.classList.remove("hidden");
  livePulse.classList.add("inline-flex");

  appendConsoleLog(
    `[System] Initializing ${payload.mode} extraction request (File Structure: ${payload.merge_option || 'default'}, Validation Gate: ${validationEnabled ? 'ACTIVE' : 'OFF/BYPASSED'})...`
  );

  try {
    const headers = { "Content-Type": "application/json" };
    if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

    const res = await fetch("/api/execute", {
      method: "POST",
      headers,
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (!res.ok) {
      isDispatching = false;
      restoreWorkflowButtons();
      queueBadge.textContent = "Queue: Idle";
      durationBadge.textContent = "Status: Idle";
      progressBar.style.width = "0%";
      livePulse.classList.add("hidden");
      livePulse.classList.remove("inline-flex");

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
    setWorkflowButtonsBusy(payload.mode === "daily" ? "daily" : "historical", "Queued in Runner...");
    queueBadge.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin text-amber-400 mr-1.5"></i> QUEUED IN RUNNER`;
    durationBadge.innerHTML = `<span class="text-amber-400 font-semibold"><i class="fa-solid fa-hourglass-start animate-spin mr-1"></i> Waiting for Runner...</span>`;

    // Fast status polls to catch the new run as soon as GitHub creates it
    setTimeout(pollJobStatus, 1200);
    setTimeout(pollJobStatus, 3000);
    setTimeout(pollJobStatus, 6000);
  } catch (err) {
    isDispatching = false;
    restoreWorkflowButtons();
    queueBadge.textContent = "Queue: Idle";
    durationBadge.textContent = "Status: Idle";
    progressBar.style.width = "0%";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");

    alert("Network request error: " + err.message);
    appendConsoleLog(`[Network Error] ${err.message}`);
  }
}

// ================= 7. LIVE STATUS, DIAGRAM ANIMATION & RESULTS =================
function startStatusPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollJobStatus();
  pollInterval = setInterval(pollJobStatus, 2500);
}

async function pollJobStatus() {
  if (!authToken) return;

  try {
    const headers = {};
    if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
    const res = await fetch("/api/status", { headers });
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

  // In-flight dispatch protection:
  // If we just dispatched, and GitHub API still returns idle or an old completed run,
  // do NOT unblock buttons or set queue to idle! The runner is still spinning up!
  if (isDispatching) {
    const isNewActiveRun = job && (job.status === "running" || job.status === "queued");
    if (isNewActiveRun) {
      isDispatching = false;
    } else if (Date.now() - lastDispatchedAt < 40000) {
      // Runner is still queuing on GitHub
      return;
    } else {
      // Safety timeout: 40s passed, unlock
      isDispatching = false;
    }
  }

  activeJobStatus = (job && job.status) ? job.status : "idle";

  if (!job || job.status === "idle") {
    queueBadge.textContent = "Queue: Idle";
    durationBadge.textContent = "Status: Idle";
    progressBar.style.width = "0%";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
    resetDiagramNodes();
    renderValidationReport(null);
    restoreWorkflowButtons();
    return;
  }

  queueBadge.textContent = `Queue: ${job.status.toUpperCase()}`;

  if (job.status === "completed") {
    const hasWarn = job.validation_report && job.validation_report.status === "warning";
    durationBadge.innerHTML = hasWarn
      ? `<i class="fa-solid fa-triangle-exclamation text-amber-400 mr-1"></i> Run #${job.run_number || job.id || 1} Completed (Warnings) &bull; 100%`
      : `<i class="fa-solid fa-circle-check text-emerald-400 mr-1"></i> Run #${job.run_number || job.id || 1} Completed &bull; 100%`;
    progressBar.style.width = "100%";
    progressBar.className = hasWarn
      ? "h-full bg-amber-500 rounded-full transition-all duration-500"
      : "h-full bg-emerald-500 rounded-full transition-all duration-500";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
    restoreWorkflowButtons();
  } else if (job.status === "failed") {
    const blocked = Boolean(job.validation_report && job.validation_report.upload_blocked);
    durationBadge.innerHTML = blocked
      ? `<i class="fa-solid fa-ban text-rose-400 mr-1"></i> Run #${job.run_number || job.id || 1} Upload Blocked by Validation`
      : `<i class="fa-solid fa-circle-xmark text-rose-400 mr-1"></i> Run #${job.run_number || job.id || 1} Failed`;
    progressBar.style.width = "100%";
    progressBar.className = "h-full bg-rose-500 rounded-full transition-all duration-500";
    livePulse.classList.add("hidden");
    livePulse.classList.remove("inline-flex");
    restoreWorkflowButtons();
  } else {
    durationBadge.textContent = `Run #${job.run_number || job.id || 1} (${job.status.toUpperCase()}) • ${job.progress_percent || 0}%`;
    progressBar.style.width = `${job.progress_percent || 0}%`;
    progressBar.className = "h-full bg-brand-500 rounded-full transition-all duration-500 animate-pulse";
    livePulse.classList.remove("hidden");
    livePulse.classList.add("inline-flex");
    setWorkflowButtonsBusy(activeTab, `Run #${job.run_number || ''} In Progress...`);
  }

  // Update Diagram Nodes and Animated Arrows
  updateDiagramColors(job);

  // Update 4 Core Table Status Badges + Validation Gate Report
  updateTableBadges(job.tables_status, job.table_details, job.validation_report);
  renderValidationReport(job);

  // Update Logs Console
  if (job.logs && job.logs.length > 0) {
    job.logs.forEach(l => appendConsoleLog(l));
  }

  // Update Final Result Banner
  renderResultBanner(job);
}

const TABLE_BADGE_MAP = [
  { key: "Revenue in Depth", reportKey: "Revenue-In-Depth", id: "tblBadge_revenue" },
  { key: "Access in Depth", reportKey: "Access-In-Depth", id: "tblBadge_access" },
  { key: "System Event in Depth", reportKey: "System-Event-In-Depth", id: "tblBadge_system" },
  { key: "Parking Transactions", reportKey: "Parking-Transactions", id: "tblBadge_parking" },
];

const TABLE_STATUS_STYLES = {
  downloading: {
    html: '<i class="fa-solid fa-spinner fa-spin mr-1"></i> Downloading',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse",
    cardClass: "p-3 bg-slate-900 border border-amber-500/30 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-amber-300/80 font-mono truncate",
  },
  done: {
    html: '<i class="fa-solid fa-check mr-1"></i> Fetched',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-semibold",
    cardClass: "p-3 bg-slate-900 border border-emerald-500/30 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-emerald-300/80 font-mono truncate",
  },
  warning: {
    html: '<i class="fa-solid fa-triangle-exclamation mr-1"></i> Warning',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-200 border border-amber-500/50 font-semibold",
    cardClass: "p-3 bg-slate-900 border border-amber-500/50 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-amber-300 font-mono truncate",
  },
  partial: {
    html: '<i class="fa-solid fa-circle-half-stroke mr-1"></i> Partial Data',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-rose-500/20 text-amber-200 border border-amber-500/60 font-bold",
    cardClass: "p-3 bg-rose-950/25 border border-amber-500/60 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-amber-200 font-mono truncate",
  },
  failed: {
    html: '<i class="fa-solid fa-xmark mr-1"></i> Failed',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-rose-500/25 text-rose-200 border border-rose-500/60 font-bold",
    cardClass: "p-3 bg-rose-950/30 border border-rose-500/60 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-rose-300 font-mono truncate",
  },
  skipped: {
    html: 'Skipped',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700",
    cardClass: "p-3 bg-slate-900 border border-slate-800 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-slate-500 font-mono truncate",
  },
  pending: {
    html: 'Pending',
    chipClass: "status-chip text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-400",
    cardClass: "p-3 bg-slate-900 border border-slate-800 rounded-xl flex flex-col gap-1.5",
    detailClass: "detail-chip text-[10px] text-slate-500 font-mono truncate",
  },
};

function updateTableBadges(tablesStatus, tableDetails, validationReport) {
  const detailsSrc = tableDetails || (validationReport && validationReport.tables) || {};

  TABLE_BADGE_MAP.forEach(({ key, reportKey, id }) => {
    const el = document.getElementById(id);
    if (!el) return;
    const chip = el.querySelector(".status-chip");
    const detailEl = el.querySelector(".detail-chip");

    const info = detailsSrc[key] || detailsSrc[reportKey] || null;
    const st = (info && info.status) || (tablesStatus && tablesStatus[key]) || "pending";
    const style = TABLE_STATUS_STYLES[st] || TABLE_STATUS_STYLES.pending;

    el.className = style.cardClass;
    if (chip) {
      chip.innerHTML = style.html;
      chip.className = style.chipClass;
    }
    if (detailEl) {
      let note = "Awaiting validation...";
      if (info && info.message) {
        note = info.message;
      } else if (info && typeof info.rows === "number") {
        note = `${Number(info.rows).toLocaleString()} rows`;
      } else if (st === "done") {
        note = "Fetched & cleaned";
      } else if (st === "downloading") {
        note = "Extracting from SKIDATA...";
      } else if (st === "failed") {
        note = "Extraction or validation error";
      }
      detailEl.textContent = note;
      detailEl.title = note;
      detailEl.className = style.detailClass;
    }
  });
}

function updateDiagramColors(job) {
  const nodeSkidata = document.getElementById("node_skidata");
  const nodeScraper = document.getElementById("node_scraper");
  const nodeTables = document.getElementById("node_tables");
  const nodeVal = document.getElementById("node_validation");
  const nodeDest = document.getElementById("node_destination");
  const arr1 = document.getElementById("arrow_1");
  const arr2 = document.getElementById("arrow_2");
  const arr3 = document.getElementById("arrow_3");
  const arr4 = document.getElementById("arrow_4");

  resetDiagramNodes();

  const setNode = (el, pillText, state) => {
    if (!el) return;
    el.className = `diagram-node flex-1 p-4 rounded-xl state-${state} text-center shadow-md`;
    const pill = el.querySelector(".node-pill");
    if (pill) pill.textContent = pillText;
  };
  const setArrow = (el, cls) => {
    if (el) el.className = `diagram-arrow ${cls} text-xl px-1`;
  };

  const report = job.validation_report || null;
  const valBlocked = Boolean(report && report.upload_blocked);
  const valBypassed = (job.validation_enabled === false) || (report && report.status === "bypassed");

  if (job.status === "failed") {
    if (valBlocked || job.active_node === "validation_gate") {
      setNode(nodeSkidata, "Connected", "completed");
      setArrow(arr1, "arrow-done");
      setNode(nodeScraper, "Completed", "completed");
      setArrow(arr2, "arrow-done");
      const hasPartial = Object.values(job.table_details || report?.tables || {}).some(t => t && t.status === "partial");
      setNode(nodeTables, hasPartial ? "Partial Tables" : "4 Tables Staged", hasPartial ? "warning" : "completed");
      setArrow(arr3, hasPartial ? "arrow-warning" : "arrow-done");
      setNode(nodeVal, "BLOCKED", "failed");
      setArrow(arr4, "arrow-failed");
      setNode(nodeDest, "Upload Blocked", "failed");
    } else {
      setNode(nodeSkidata, "Connected", "completed");
      setArrow(arr1, "arrow-done");
      setNode(nodeScraper, "Failed", "failed");
      setNode(nodeVal, valBypassed ? "Bypassed" : "Halted", valBypassed ? "bypassed" : "failed");
      setArrow(arr4, "arrow-failed");
      setNode(nodeDest, "Aborted", "failed");
    }
    return;
  }

  if (job.status === "completed") {
    setNode(nodeSkidata, "Connected", "completed");
    setArrow(arr1, "arrow-done");
    setNode(nodeScraper, "Completed", "completed");
    setArrow(arr2, "arrow-done");
    setNode(nodeTables, "All 4 Tables Ready", "completed");
    setArrow(arr3, "arrow-done");

    if (valBypassed) {
      setNode(nodeVal, "Bypassed (Off)", "bypassed");
      setArrow(arr4, "arrow-done");
      setNode(nodeDest, "Uploaded (Fast)", "completed");
    } else if (report && report.status === "warning") {
      setNode(nodeVal, "Soft Warnings", "warning");
      setArrow(arr4, "arrow-warning");
      setNode(nodeDest, "Saved w/ Advisory", "completed");
    } else {
      setNode(nodeVal, "Verified", "completed");
      setArrow(arr4, "arrow-done");
      setNode(nodeDest, "Verified & Saved", "completed");
    }
    return;
  }

  const active = job.active_node || "";
  const completed = job.completed_nodes || [];

  // Node 1: SkiData Hub
  if (active === "skidata_auth") {
    setNode(nodeSkidata, "Authenticating...", "running");
    setArrow(arr1, "arrow-active");
  } else if (completed.includes("skidata_auth") || active) {
    setNode(nodeSkidata, "Connected", "completed");
    setArrow(arr1, "arrow-done");
  }

  // Node 2: Scraper Engine
  if (active === "scraper_engine") {
    setNode(nodeScraper, "Scraping Active", "running");
    setArrow(arr2, "arrow-active");
  } else if (
    completed.includes("scraper_engine") ||
    active.startsWith("table_") ||
    active === "validation_gate" ||
    active === "destination_upload"
  ) {
    setNode(nodeScraper, "Completed", "completed");
    setArrow(arr2, "arrow-done");
  }

  // Node 3: Tables Ingestion
  if (active.startsWith("table_")) {
    let lbl = "Fetching Tables";
    if (active === "table_revenue") lbl = "Table 1/4 (Revenue)";
    else if (active === "table_access") lbl = "Table 2/4 (Access)";
    else if (active === "table_system") lbl = "Table 3/4 (System Event)";
    else if (active === "table_parking") lbl = "Table 4/4 (Parking)";

    setNode(nodeTables, lbl, "running");
    setArrow(arr2, "arrow-done");
    setArrow(arr3, "arrow-active");
  } else if (
    completed.includes("table_parking") ||
    active === "validation_gate" ||
    active === "destination_upload"
  ) {
    setNode(nodeTables, "All 4 Tables Ready", "completed");
    setArrow(arr3, "arrow-done");
  }

  // Node 4: Validation Gate
  if (active === "validation_gate") {
    setNode(nodeVal, "Validating...", "running");
    setArrow(arr4, "arrow-active");
  } else if (completed.includes("validation_gate") || active === "destination_upload") {
    if (valBypassed) {
      setNode(nodeVal, "Bypassed", "bypassed");
    } else {
      setNode(nodeVal, "Passed", "completed");
    }
    setArrow(arr4, "arrow-done");
  } else if (valBypassed) {
    setNode(nodeVal, "Off (Bypass)", "bypassed");
  } else {
    setNode(nodeVal, "Armed", "idle");
  }

  // Node 5: Destination Upload
  if (active === "destination_upload") {
    setNode(nodeDest, "Uploading...", "running");
    setArrow(arr4, "arrow-active");
  }
}

function resetDiagramNodes() {
  document.querySelectorAll(".diagram-node").forEach(n => {
    n.className = "diagram-node flex-1 p-4 rounded-xl state-idle text-center shadow-md";
    const pill = n.querySelector(".node-pill");
    if (pill) pill.textContent = (n.id === "node_validation") ? (validationEnabled ? "Armed" : "Off") : "Idle";
  });
  document.querySelectorAll(".diagram-arrow").forEach(a => {
    a.className = "diagram-arrow text-slate-600 text-xl px-1";
  });
  if (!validationEnabled) {
    const nodeVal = document.getElementById("node_validation");
    if (nodeVal) nodeVal.className = "diagram-node flex-1 p-4 rounded-xl state-bypassed text-center shadow-md";
  }
}

function setValidationCheckCard(cardId, status, detailText) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const badge = card.querySelector(".val-card-badge");
  const detail = card.querySelector(".val-card-detail");

  const cardThemes = {
    passed: {
      border: "border-emerald-500/35 bg-emerald-950/20",
      badge: "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
      label: "PASS",
    },
    warning: {
      border: "border-amber-500/45 bg-amber-950/25",
      badge: "bg-amber-500/20 text-amber-200 border border-amber-500/40",
      label: "SOFT WARN",
    },
    failed: {
      border: "border-rose-500/60 bg-rose-950/35",
      badge: "bg-rose-500/25 text-rose-200 border border-rose-500/50",
      label: "HARD FAIL",
    },
    bypassed: {
      border: "border-slate-700 border-dashed bg-slate-900/60",
      badge: "bg-slate-800 text-amber-300 border border-amber-500/30",
      label: "BYPASSED",
    },
    skipped: {
      border: "border-slate-800 bg-slate-950/80",
      badge: "bg-slate-800 text-slate-400",
      label: "SKIPPED",
    },
  };

  const t = cardThemes[status] || cardThemes.skipped;
  card.className = `p-2.5 rounded-lg border space-y-1 ${t.border}`;
  if (badge) {
    badge.className = `val-card-badge text-[9px] px-2 py-0.5 rounded uppercase font-mono font-semibold ${t.badge}`;
    badge.textContent = t.label;
  }
  if (detail) {
    detail.textContent = detailText || "-";
    detail.title = detailText || "";
  }
}

function renderFindingsList(boxId, listId, items, severity) {
  const box = document.getElementById(boxId);
  const list = document.getElementById(listId);
  if (!box || !list) return;

  if (!items || items.length === 0) {
    box.classList.add("hidden");
    list.innerHTML = "";
    return;
  }

  const badgeCls = severity === "hard"
    ? "px-1.5 py-0.5 rounded bg-rose-500/30 text-rose-200 border border-rose-500/40 text-[10px] font-bold"
    : "px-1.5 py-0.5 rounded bg-amber-500/25 text-amber-200 border border-amber-500/40 text-[10px] font-bold";

  list.innerHTML = items.map(item => `
    <li class="flex items-start gap-2 leading-relaxed">
      <span class="${badgeCls} shrink-0">${escapeHtml(item.code || "CHECK")}</span>
      <span>${escapeHtml(item.message || "")}</span>
    </li>
  `).join("");
  box.classList.remove("hidden");
}

function renderValidationReport(job, opts = {}) {
  const card = document.getElementById("validationReportCard");
  const iconEl = document.getElementById("valReportIcon");
  const verdictEl = document.getElementById("valReportVerdictBadge");
  const summaryEl = document.getElementById("valReportSummaryText");
  const compGrid = document.getElementById("valComponentGrid");
  const auditBar = document.getElementById("valAuditMetaBar");
  const toggleBtn = document.getElementById("valToggleJsonBtn");
  const copyBtn = document.getElementById("valCopyJsonBtn");
  const downloadBtn = document.getElementById("valDownloadJsonBtn");
  const jsonWrapper = document.getElementById("valJsonWrapper");
  const jsonPre = document.getElementById("valJsonPre");

  if (!card || !verdictEl || !summaryEl) return;

  const setBannerTheme = (cardCls, iconCls, iconHtml, badgeCls, badgeText, summaryText) => {
    card.className = `p-4 rounded-xl border space-y-3 transition-all duration-300 ${cardCls}`;
    if (iconEl) {
      iconEl.className = `w-7 h-7 rounded-lg flex items-center justify-center text-xs ${iconCls}`;
      iconEl.innerHTML = iconHtml;
    }
    verdictEl.className = `text-[10px] px-2.5 py-0.5 rounded-full border font-semibold uppercase tracking-wide ${badgeCls}`;
    verdictEl.textContent = badgeText;
    summaryEl.textContent = summaryText;
  };

  const hideExtras = () => {
    latestValidationReport = null;
    valJsonLastReportKey = null;
    if (compGrid) compGrid.classList.add("hidden");
    if (auditBar) auditBar.classList.add("hidden");
    if (toggleBtn) toggleBtn.classList.add("hidden");
    if (copyBtn) copyBtn.classList.add("hidden");
    if (downloadBtn) downloadBtn.classList.add("hidden");
    if (jsonWrapper) jsonWrapper.classList.add("hidden");
    renderFindingsList("valHardFailuresBox", "valHardFailuresList", [], "hard");
    renderFindingsList("valSoftWarningsBox", "valSoftWarningsList", [], "soft");
  };

  if (!job || job.status === "idle") {
    hideExtras();
    const isEnabled = opts.enabled !== undefined ? opts.enabled : validationEnabled;
    if (opts.pending) {
      setBannerTheme(
        "bg-slate-900/90 border-sky-500/40",
        "bg-sky-500/15 text-sky-400",
        '<i class="fa-solid fa-circle-notch fa-spin"></i>',
        "bg-sky-500/15 text-sky-300 border-sky-500/30",
        isEnabled ? "Queued & Armed" : "Queued (Gate Off)",
        isEnabled
          ? "Validation Gate is armed for this run. Checks execute automatically before SharePoint upload."
          : "Validation Gate is switched OFF for this run (time-constraint bypass mode)."
      );
    } else if (isEnabled) {
      setBannerTheme(
        "bg-slate-900/90 border-slate-800",
        "bg-emerald-500/15 text-emerald-400",
        '<i class="fa-solid fa-shield-halved"></i>',
        "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
        "Armed & Active",
        "Pre-upload audit validates full date coverage, Excel cell types, financial reconciliation & anomalies."
      );
    } else {
      setBannerTheme(
        "bg-amber-950/20 border-amber-500/40 border-dashed",
        "bg-amber-500/15 text-amber-400",
        '<i class="fa-solid fa-bolt"></i>',
        "bg-amber-500/15 text-amber-300 border-amber-500/40",
        "Off (Time-Constraint Bypass)",
        "Validation Gate is turned OFF. Next launch will skip completeness, schema & financial verification checks."
      );
    }
    renderDataInsights(job);
    return;
  }

  if (job.status === "running" || job.status === "queued") {
    hideExtras();
    const runEnabled = job.validation_enabled !== false;
    setBannerTheme(
      runEnabled ? "bg-slate-900/90 border-sky-500/40" : "bg-amber-950/20 border-amber-500/40 border-dashed",
      runEnabled ? "bg-sky-500/15 text-sky-400" : "bg-amber-500/15 text-amber-400",
      '<i class="fa-solid fa-shield-halved animate-pulse"></i>',
      runEnabled ? "bg-sky-500/15 text-sky-300 border-sky-500/30" : "bg-amber-500/15 text-amber-300 border-amber-500/40",
      runEnabled ? "Armed (Run In Progress)" : "Off (Bypassed Run)",
      runEnabled
        ? "Extracting & cleaning tables now. Full Validation Gate report will appear automatically once checks finish."
        : "Validation Gate disabled for this run to prioritize download speed."
    );
    renderDataInsights(job);
    return;
  }

  const report = job.validation_report || null;
  if (!report) {
    hideExtras();
    if (job.status === "failed") {
      setBannerTheme(
        "bg-rose-950/35 border-rose-500/50",
        "bg-rose-500/20 text-rose-400",
        '<i class="fa-solid fa-circle-exclamation"></i>',
        "bg-rose-500/20 text-rose-200 border-rose-500/50",
        "Caught Exception / Aborted",
        job.error || "Pipeline stopped before Validation Gate produced _validation_report.json; SharePoint upload did not complete."
      );
      renderFindingsList(
        "valHardFailuresBox",
        "valHardFailuresList",
        [{ code: "PIPELINE_EXCEPTION", message: job.error || "Job aborted with uncaught failure; SharePoint upload blocked." }],
        "hard"
      );
    } else if (job.validation_enabled === false) {
      setBannerTheme(
        "bg-slate-900/90 border-amber-500/40 border-dashed",
        "bg-amber-500/15 text-amber-400",
        '<i class="fa-solid fa-bolt"></i>',
        "bg-amber-500/20 text-amber-200 border-amber-500/40",
        "Validation Bypassed (Off)",
        "Run completed with Validation Gate turned OFF."
      );
    } else {
      setBannerTheme(
        "bg-slate-900/90 border-emerald-500/30",
        "bg-emerald-500/15 text-emerald-400",
        '<i class="fa-solid fa-circle-check"></i>',
        "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
        "Completed",
        "Workflow completed successfully (no validation-report artifact attached for this run mode)."
      );
    }
    renderDataInsights(job);
    return;
  }

  // Full structured _validation_report.json available inside UI (never uploaded to SharePoint)
  latestValidationReport = report;
  if (jsonPre) jsonPre.textContent = JSON.stringify(report, null, 2);
  if (downloadBtn) downloadBtn.classList.remove("hidden");
  if (toggleBtn) toggleBtn.classList.remove("hidden");
  if (copyBtn) copyBtn.classList.remove("hidden");
  if (compGrid) compGrid.classList.remove("hidden");

  const reportKey = `${report.generated_at || ""}|${report.file_prefix || ""}|${report.status || ""}`;
  if (jsonWrapper && reportKey !== valJsonLastReportKey) {
    valJsonLastReportKey = reportKey;
    jsonWrapper.classList.remove("hidden");
    updateValJsonToggleLabel(false);
  } else if (jsonWrapper) {
    updateValJsonToggleLabel(jsonWrapper.classList.contains("hidden"));
  }

  const rStatus = report.status || "passed";
  if (auditBar) {
    const recCheck = (report.checks && report.checks.reconciliation) || {};
    const compCheck = (report.checks && report.checks.completeness) || {};
    const spDelivery = report.upload_blocked
      ? '<span class="text-rose-300 font-semibold">Blocked (.xlsx held)</span>'
      : report.upload_performed
        ? '<span class="text-emerald-300 font-semibold">Excel (.xlsx) Only &middot; JSON in UI</span>'
        : '<span class="text-slate-300 font-semibold">Local Stage Only</span>';
    const windowText = report.start_date && report.end_date
      ? `${escapeHtml(report.start_date)} &rarr; ${escapeHtml(report.end_date)}`
      : escapeHtml(report.file_prefix || "-");
    auditBar.classList.remove("hidden");
    auditBar.innerHTML = `
      <div><span class="text-slate-500 block text-[10px]">Batch Window</span><span class="text-slate-200 font-mono">${windowText}</span></div>
      <div><span class="text-slate-500 block text-[10px]">SharePoint Payload</span>${spDelivery}</div>
      <div><span class="text-slate-500 block text-[10px]">Financial Totals</span><span class="text-slate-200 font-mono">Theo ${Number(recCheck.revenue_theoretical || 0).toLocaleString()} &middot; Gross ${Number(recCheck.revenue_gross || 0).toLocaleString()}</span></div>
      <div><span class="text-slate-500 block text-[10px]">Row Ceiling Check</span><span class="text-slate-200 font-mono">Max ${Number(compCheck.max_export_rows || 0).toLocaleString()} / 100,000</span></div>
    `;
  }
  if (rStatus === "failed" || report.upload_blocked) {
    setBannerTheme(
      "bg-rose-950/35 border-rose-500/60 shadow-lg shadow-rose-950/30",
      "bg-rose-500/20 text-rose-400",
      '<i class="fa-solid fa-ban"></i>',
      "bg-rose-500/25 text-rose-200 border-rose-500/60",
      "SharePoint Upload Blocked",
      report.summary || "Critical data validation failure blocked SharePoint upload."
    );
  } else if (rStatus === "warning") {
    setBannerTheme(
      "bg-amber-950/25 border-amber-500/50",
      "bg-amber-500/20 text-amber-300",
      '<i class="fa-solid fa-triangle-exclamation"></i>',
      "bg-amber-500/20 text-amber-200 border-amber-500/50",
      "Passed With Soft Warnings",
      report.summary || "Data uploaded cleanly with soft advisory warnings."
    );
  } else if (rStatus === "bypassed") {
    setBannerTheme(
      "bg-slate-900/90 border-amber-500/40 border-dashed",
      "bg-amber-500/15 text-amber-400",
      '<i class="fa-solid fa-bolt"></i>',
      "bg-amber-500/20 text-amber-200 border-amber-500/40",
      "Bypassed (Gate Off)",
      report.summary || "Validation checks skipped by operator; files uploaded directly."
    );
  } else {
    setBannerTheme(
      "bg-emerald-950/20 border-emerald-500/45",
      "bg-emerald-500/20 text-emerald-400",
      '<i class="fa-solid fa-shield-halved"></i>',
      "bg-emerald-500/20 text-emerald-200 border-emerald-500/40",
      "Verified & Clean",
      report.summary || "All completeness, Excel type, and financial reconciliation checks passed."
    );
  }

  // Render 4 tier summary cards
  const checks = report.checks || {};
  const comp = checks.completeness || {};
  const expD = comp.expected_days ?? 0;
  const covD = comp.covered_days ?? 0;
  const emptyCount = Array.isArray(comp.portal_empty_days) ? comp.portal_empty_days.length : 0;
  const missCount = Array.isArray(comp.missing_days) ? comp.missing_days.length : 0;
  const compTxt = rStatus === "bypassed"
    ? "Bypassed"
    : `${covD}/${expD} days · miss ${missCount}${emptyCount ? ` · empty ${emptyCount}` : ""}${comp.qlik_100k_risk ? " · 100k cap!" : ""}`;
  setValidationCheckCard("valCard_completeness", comp.status || (rStatus === "bypassed" ? "bypassed" : "skipped"), compTxt);

  const st = checks.structure_and_types || {};
  const vc = st.violation_counts || {};
  const totalTypeErr = (vc.barcodes || 0) + (vc.dates || 0) + (vc.durations || 0) + (vc.numbers || 0) + (vc.na_stripped || 0) + (vc.totals_left || 0);
  const stTxt = rStatus === "bypassed"
    ? "Bypassed"
    : totalTypeErr > 0
      ? `${totalTypeErr} XML/type violation(s)`
      : `Clean XML · NA=${Number(st.na_count || 0).toLocaleString()}`;
  setValidationCheckCard("valCard_schema", st.status || (rStatus === "bypassed" ? "bypassed" : "skipped"), stTxt);

  const rec = checks.reconciliation || {};
  let recSuffix = "";
  if (rec.pre_clean_totals_match === false) recSuffix = " · Pre-clean Totals mismatch";
  else if (Array.isArray(rec.missing_in_parking_dates) && rec.missing_in_parking_dates.length > 0) {
    recSuffix = ` · Missing PT day (${rec.missing_in_parking_dates.length})`;
  }
  const recTxt = rStatus === "bypassed"
    ? "Bypassed"
    : rec.status === "skipped"
      ? "Skipped (single-table filter)"
      : `Rev ${Number(rec.revenue_gross || 0).toLocaleString()} vs Pay ${Number(rec.parking_payment || 0).toLocaleString()} (${rec.revenue_diff_pct ?? 0}%)${recSuffix}`;
  setValidationCheckCard("valCard_reconciliation", rec.status || (rStatus === "bypassed" ? "bypassed" : "skipped"), recTxt);

  const anom = checks.anomalies || {};
  const anomTxt = rStatus === "bypassed"
    ? "Bypassed"
    : `Dup ${anom.duplicate_composite_rows || 0} · OOR ${anom.out_of_range_txn_dates_count || 0} · Exit<Entry ${anom.inverted_exit_lt_entry_count || 0}`;
  setValidationCheckCard("valCard_anomalies", anom.status || (rStatus === "bypassed" ? "bypassed" : "skipped"), anomTxt);

  // Render hard failures & soft warnings callouts
  const hardList = Array.isArray(report.hard_failures) ? [...report.hard_failures] : [];
  if ((rStatus === "failed" || job.status === "failed") && hardList.length === 0) {
    hardList.push({
      code: "EXCEPTION",
      message: report.scrape_error || job.error || "Execution failed cleanly before upload.",
    });
  }
  renderFindingsList("valHardFailuresBox", "valHardFailuresList", hardList, "hard");
  renderFindingsList("valSoftWarningsBox", "valSoftWarningsList", report.soft_warnings || [], "soft");
  renderDataInsights(job);
}

function destroyInsightCharts() {
  for (const key of Object.keys(insightCharts)) {
    if (insightCharts[key] && typeof insightCharts[key].destroy === "function") {
      insightCharts[key].destroy();
    }
    insightCharts[key] = null;
  }
}

function renderOrReplaceChart(chartKey, canvasId, boxId, chartConfig, fallbackItems) {
  const canvas = document.getElementById(canvasId);
  const box = document.getElementById(boxId);
  if (!canvas || !box) return;

  if (insightCharts[chartKey] && typeof insightCharts[chartKey].destroy === "function") {
    insightCharts[chartKey].destroy();
    insightCharts[chartKey] = null;
  }

  if (typeof window.Chart !== "function") {
    // Graceful HTML bar fallback if Chart.js CDN is blocked/offline
    const rows = fallbackItems || [];
    const maxVal = Math.max(1, ...rows.map(r => Number(r.value || 0)));
    box.innerHTML = `<div class="space-y-1.5 overflow-y-auto max-h-full pr-1">${
      rows.slice(0, 12).map(r => {
        const pct = Math.max(2, Math.round((Number(r.value || 0) / maxVal) * 100));
        return `
          <div class="text-[10px] font-mono">
            <div class="flex justify-between text-slate-300 mb-0.5">
              <span>${escapeHtml(r.label)}</span><span>${Number(r.value || 0).toLocaleString()}</span>
            </div>
            <div class="h-1.5 bg-slate-800 rounded overflow-hidden">
              <div class="h-full bg-emerald-500 rounded" style="width:${pct}%"></div>
            </div>
          </div>`;
      }).join("") || '<p class="text-[11px] text-slate-500">No data points available.</p>'
    }</div>`;
    return;
  }

  insightCharts[chartKey] = new window.Chart(canvas.getContext("2d"), chartConfig);
}

function styleInsightButtonGroup(selector, activeKey, dataAttr) {
  document.querySelectorAll(selector).forEach((btn) => {
    const isActive = btn.dataset[dataAttr] === activeKey;
    btn.className = isActive
      ? `${selector.slice(1)} px-2.5 py-1 rounded-lg text-[11px] font-semibold bg-sky-500/20 text-sky-300 border border-sky-500/40 transition`
      : `${selector.slice(1)} px-2.5 py-1 rounded-lg text-[11px] font-medium text-slate-400 hover:text-white border border-transparent transition`;
  });
}

function renderDataInsights(job) {
  latestInsightsJob = job;
  const scopeBadge = document.getElementById("insightsScopeBadge");
  const viewTabs = document.getElementById("insightViewTabs");
  const emptyState = document.getElementById("insightsEmptyState");
  const emptyMsg = document.getElementById("insightsEmptyMsg");
  const body = document.getElementById("insightsDashboardBody");
  if (!scopeBadge || !emptyState || !body) return;

  const report = (job && job.validation_report) || null;
  const ins = (report && report.insights) || null;

  if (!ins) {
    destroyInsightCharts();
    insightLastReportKey = null;
    insightLastDrawSignature = null;
    body.classList.add("hidden");
    if (viewTabs) viewTabs.classList.add("hidden");
    emptyState.classList.remove("hidden");

    if (job && (job.status === "running" || job.status === "queued")) {
      scopeBadge.textContent = "Generating...";
      if (emptyMsg) emptyMsg.textContent = "Extracting & validating tables — interactive Insight Graphs will render automatically once Validation Gate completes.";
    } else if (report && report.status === "bypassed") {
      scopeBadge.textContent = "Gate Off (Bypassed)";
      if (emptyMsg) emptyMsg.textContent = "Insight Graphs are skipped while Validation Gate is OFF (fast bypass mode). Switch Validation Gate ON for analytics.";
    } else {
      scopeBadge.textContent = "Awaiting Fetch";
      if (emptyMsg) emptyMsg.textContent = "Run a Day, Date Range, or Month extraction with Validation Gate enabled to visualize fetched insights.";
    }
    return;
  }

  const reportKey = `${report.generated_at || ""}|${report.file_prefix || ""}|${report.batch_name || ""}`;
  if (reportKey !== insightLastReportKey) {
    insightLastReportKey = reportKey;
    insightActiveView = ins.default_view || "daily";
    insightLastDrawSignature = null;
  }

  if (viewTabs) viewTabs.classList.remove("hidden");
  emptyState.classList.add("hidden");
  body.classList.remove("hidden");
  styleInsightButtonGroup(".insight-view-tab", insightActiveView, "insightView");
  styleInsightButtonGroup(".insight-cp-tab", insightCpMetric, "cpMetric");

  const k = ins.kpis || {};
  const batchCount = Array.isArray(ins.batches_series) ? ins.batches_series.length : 0;
  if (batchCount > 1) {
    scopeBadge.textContent = `${batchCount} Months · ${k.days_count || 0} Days`;
  } else if ((k.days_count || 0) <= 1) {
    scopeBadge.textContent = `Single Day (${report.start_date || report.file_prefix || "1d"}) · Hourly`;
  } else {
    scopeBadge.textContent = `${k.days_count} Days (${report.start_date || ""} -> ${report.end_date || ""})`;
  }

  // Update 4 executive KPI tiles
  const setText = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  const gross = Number(k.revenue_gross || 0);
  const pay = Number(k.parking_payment || 0);
  const theo = Number(k.revenue_theoretical || 0);
  const disc = Number(k.total_discount || 0);
  const revDisplay = gross > 0 ? gross : pay;
  setText("insKpiRevMain", `${revDisplay.toLocaleString()} SAR`);
  setText("insKpiRevSub", `PT Pay ${pay.toLocaleString()} · Theo ${theo.toLocaleString()} · Disc ${disc.toLocaleString()}`);

  const totRows = Number(k.total_rows || 0);
  const paidRows = Number(k.paid_rows || 0);
  const freeRows = Number(k.free_rows || 0);
  setText("insKpiRowsMain", `${totRows.toLocaleString()} txns`);
  setText("insKpiRowsSub", `${paidRows.toLocaleString()} paid (${k.paid_pct ?? 0}%) · ${freeRows.toLocaleString()} free`);

  const entries = Number(k.access_entries || 0);
  const exits = Number(k.access_exits || 0);
  const pkHour = k.peak_hour;
  const pkDay = k.peak_day;
  const isSingleDayView = (k.days_count || 0) <= 1 || insightActiveView === "hourly";
  const peakMain = entries > 0
    ? `${entries.toLocaleString()} entries`
    : pkHour && isSingleDayView
      ? `Peak ${pkHour.hour}`
      : pkDay
        ? `Peak ${pkDay.date}`
        : "-";
  const peakSub = isSingleDayView && pkHour
    ? `Peak Hr ${pkHour.hour} (${Number(pkHour.revenue_gross || pkHour.parking_payment || 0).toLocaleString()} SAR)`
    : pkDay
      ? `Peak Day ${pkDay.date} (${Number(pkDay.revenue_gross || pkDay.parking_payment || 0).toLocaleString()} SAR)${exits ? ` · Exits ${exits.toLocaleString()}` : ""}`
      : "No gate metrics";
  setText("insKpiPeakMain", peakMain);
  setText("insKpiPeakSub", peakSub);

  const topCp = k.top_car_park;
  setText("insKpiTopCpMain", topCp ? topCp.car_park : "-");
  setText(
    "insKpiTopCpSub",
    topCp
      ? `${Number(topCp.revenue_gross || topCp.parking_payment || 0).toLocaleString()} SAR (${topCp.share_pct ?? 0}% share) · ${Number(topCp.parking_rows || 0).toLocaleString()} txns`
      : "No car park breakdown"
  );

  // Skip heavy canvas teardown/rebuild when data & active tabs are identical across 5s polls
  const drawSignature = `${reportKey}|${insightActiveView}|${insightCpMetric}`;
  if (drawSignature === insightLastDrawSignature) return;
  insightLastDrawSignature = drawSignature;

  // 1. Dynamic Time-Series Chart (Daily / Monthly / Hourly)
  const daily = ins.daily_series || [];
  const monthly = (Array.isArray(ins.batches_series) && ins.batches_series.length > 1)
    ? ins.batches_series
    : (ins.monthly_series || []).map(m => ({ ...m, label: m.month }));
  const hourly = ins.hourly_series || [];

  let timeLabels = [];
  let revSeries = [];
  let paySeries = [];
  let volSeries = [];
  let timeFallback = [];
  if (insightActiveView === "hourly") {
    setText("insTimeChartTitle", "24-Hour Peak Revenue & Barrier Traffic Profile");
    setText("insTimeChartHint", "Best for Day-wise & Rush-Hour analysis · Green/Cyan: SAR · Dashed Indigo: Txns / Entries");
    timeLabels = hourly.map(h => h.hour);
    revSeries = hourly.map(h => h.revenue_gross || 0);
    paySeries = hourly.map(h => h.parking_payment || 0);
    volSeries = hourly.map(h => h.parking_rows || h.access_entries || 0);
    timeFallback = hourly.map(h => ({ label: h.hour, value: h.revenue_gross || h.parking_payment || h.parking_rows || 0 }));
  } else if (insightActiveView === "monthly") {
    setText("insTimeChartTitle", "Monthly / Batch Revenue, Payment & Volume Rollup");
    setText("insTimeChartHint", "Compares Gross Revenue vs PT Payment (SAR) & total Parking Volume per month/batch");
    timeLabels = monthly.map(m => m.label || m.month);
    revSeries = monthly.map(m => m.revenue_gross || 0);
    paySeries = monthly.map(m => m.parking_payment || 0);
    volSeries = monthly.map(m => m.parking_rows || m.access_entries || 0);
    timeFallback = monthly.map(m => ({ label: m.label || m.month, value: m.revenue_gross || m.parking_payment || m.parking_rows || 0 }));
  } else {
    setText("insTimeChartTitle", "Daily Revenue vs Payment & Parking Volume Trend");
    setText("insTimeChartHint", "Drops to 0 expose missing dates visually · Green/Cyan: SAR · Dashed Indigo: Parking Sessions");
    timeLabels = daily.map(d => d.date.slice(5));
    revSeries = daily.map(d => d.revenue_gross || 0);
    paySeries = daily.map(d => d.parking_payment || 0);
    volSeries = daily.map(d => d.parking_rows || d.access_entries || 0);
    timeFallback = daily.map(d => ({ label: d.date, value: d.revenue_gross || d.parking_payment || d.parking_rows || 0 }));
  }

  renderOrReplaceChart("time", "insightTimeChart", "insTimeChartBox", {
    type: "bar",
    data: {
      labels: timeLabels,
      datasets: [
        {
          label: "Gross Revenue (SAR)",
          data: revSeries,
          backgroundColor: "rgba(34, 197, 94, 0.72)",
          borderColor: "#22c55e",
          borderWidth: 1,
          borderRadius: 4,
          yAxisID: "y",
        },
        {
          label: "PT Payment (SAR)",
          data: paySeries,
          backgroundColor: "rgba(14, 165, 233, 0.68)",
          borderColor: "#0ea5e9",
          borderWidth: 1,
          borderRadius: 4,
          yAxisID: "y",
        },
        {
          label: "Volume (Txns / Entries)",
          data: volSeries,
          type: "line",
          borderColor: "#a78bfa",
          backgroundColor: "rgba(167, 139, 250, 0.15)",
          borderWidth: 2,
          borderDash: [4, 3],
          pointRadius: timeLabels.length > 25 ? 1.5 : 3,
          tension: 0.25,
          yAxisID: "y1",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#cbd5e1", font: { size: 10 } } },
      },
      scales: {
        x: { ticks: { color: "#94a3b8", font: { size: 10 }, maxRotation: 45 }, grid: { color: "rgba(51,65,85,0.25)" } },
        y: {
          position: "left",
          beginAtZero: true,
          ticks: { color: "#94a3b8", font: { size: 10 } },
          grid: { color: "rgba(51,65,85,0.35)" },
          title: { display: true, text: "SAR", color: "#94a3b8", font: { size: 10 } },
        },
        y1: {
          position: "right",
          beginAtZero: true,
          ticks: { color: "#a78bfa", font: { size: 10 } },
          grid: { drawOnChartArea: false },
          title: { display: true, text: "Count", color: "#a78bfa", font: { size: 10 } },
        },
      },
    },
  }, timeFallback);

  // 2. Payment Method Mix Doughnut
  const methods = ins.payment_methods || [];
  const palette = ["#22c55e", "#0ea5e9", "#f59e0b", "#a78bfa", "#f43f5e", "#14b8a6"];
  setText("insPaidRatioPill", `${k.paid_pct ?? 0}% Paid (${paidRows.toLocaleString()})`);
  const mixLegend = document.getElementById("insMixLegendList");
  if (mixLegend) {
    mixLegend.innerHTML = methods.length
      ? methods.slice(0, 5).map((m, idx) => `
          <li class="flex items-center justify-between gap-2">
            <span class="flex items-center gap-1.5 truncate">
              <span class="w-2 h-2 rounded-full shrink-0" style="background:${palette[idx % palette.length]}"></span>
              <span class="truncate">${escapeHtml(m.method)}</span>
            </span>
            <span class="text-slate-400 shrink-0">${Number(m.amount || 0).toLocaleString()} SAR (${Number(m.count || 0).toLocaleString()})</span>
          </li>`).join("")
      : '<li class="text-slate-500">No paid tender breakdown in this filter.</li>';
  }
  renderOrReplaceChart("mix", "insightMixChart", "insMixChartBox", {
    type: "doughnut",
    data: {
      labels: methods.map(m => m.method),
      datasets: [{
        data: methods.map(m => m.amount || 0),
        backgroundColor: methods.map((_, idx) => palette[idx % palette.length]),
        borderColor: "#0f172a",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      plugins: { legend: { display: false } },
    },
  }, methods.map(m => ({ label: m.method, value: m.amount || 0 })));

  // 3. Car Park Breakdown Chart (Financial SAR vs Traffic Switcher)
  const cps = (ins.by_car_park || []).slice(0, 12);
  const isTrafficCp = insightCpMetric === "traffic";
  setText("insCpChartHint", isTrafficCp
    ? "Parking-Transactions row counts vs Access-In-Depth Entries by site"
    : "Compare Revenue-In-Depth Gross Revenue vs Parking-Transactions Payment across locations");
  renderOrReplaceChart("carPark", "insightCarParkChart", "insCpChartBox", {
    type: "bar",
    data: {
      labels: cps.map(c => c.car_park),
      datasets: isTrafficCp
        ? [
            { label: "Parking Rows", data: cps.map(c => c.parking_rows || 0), backgroundColor: "rgba(14, 165, 233, 0.72)", borderRadius: 4 },
            { label: "Access Entries", data: cps.map(c => c.access_entries || 0), backgroundColor: "rgba(167, 139, 250, 0.72)", borderRadius: 4 },
          ]
        : [
            { label: "Gross Revenue (SAR)", data: cps.map(c => c.revenue_gross || 0), backgroundColor: "rgba(34, 197, 94, 0.75)", borderRadius: 4 },
            { label: "PT Payment (SAR)", data: cps.map(c => c.parking_payment || 0), backgroundColor: "rgba(14, 165, 233, 0.72)", borderRadius: 4 },
          ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#cbd5e1", font: { size: 10 } } } },
      scales: {
        x: { ticks: { color: "#cbd5e1", font: { size: 10 } }, grid: { color: "rgba(51,65,85,0.2)" } },
        y: { beginAtZero: true, ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(51,65,85,0.35)" } },
      },
    },
  }, cps.map(c => ({ label: c.car_park, value: isTrafficCp ? (c.parking_rows || c.access_entries || 0) : (c.revenue_gross || c.parking_payment || 0) })));
}

function renderResultBanner(job) {
  const banner = document.getElementById("executionResultBanner");
  if (!banner) return;

  const report = job.validation_report || null;
  if (job.status === "completed") {
    banner.classList.remove("hidden");
    const targetFolder = document.getElementById("diagDestFolderText").textContent || "Validated_Docs/old";
    if (report && report.status === "warning") {
      const warnCount = (report.soft_warnings || []).length;
      banner.className = "p-4 rounded-xl border text-xs bg-amber-500/15 border-amber-500/40 text-amber-100 space-y-1";
      banner.innerHTML = `
        <div class="flex items-center gap-2 font-bold text-amber-300 text-sm">
          <i class="fa-solid fa-triangle-exclamation"></i>
          <span>Extraction Finished with ${warnCount} Soft Warning(s) &mdash; Delivered with Advisory</span>
        </div>
        <p>${escapeHtml(report.summary || "Excel reports uploaded to SharePoint; soft advisory notes kept in the UI Validation Report.")}</p>
        <p class="text-[11px] text-amber-200/80">Delivered to SharePoint folder: <strong>${escapeHtml(targetFolder)}</strong> (.xlsx files only &mdash; validation JSON kept in UI).</p>
      `;
    } else if (job.validation_enabled === false || (report && report.status === "bypassed")) {
      banner.className = "p-4 rounded-xl border text-xs bg-slate-800/80 border-amber-500/40 text-slate-200 space-y-1";
      banner.innerHTML = `
        <div class="flex items-center gap-2 font-bold text-amber-300 text-sm">
          <i class="fa-solid fa-bolt"></i>
          <span>Extraction Completed (Validation Gate Off &mdash; Fast Bypass)</span>
        </div>
        <p>Files were uploaded without running the Validation Gate as requested by the header switch.</p>
        <p class="text-[11px] text-slate-400">Delivered to SharePoint folder: <strong>${escapeHtml(targetFolder)}</strong>.</p>
      `;
    } else {
      banner.className = "p-4 rounded-xl border text-xs bg-emerald-500/15 border-emerald-500/30 text-emerald-200 space-y-1";
      banner.innerHTML = `
        <div class="flex items-center gap-2 font-bold text-emerald-300 text-sm">
          <i class="fa-solid fa-circle-check"></i>
          <span>Extraction Finished &amp; Validated Successfully!</span>
        </div>
        <p>All 4 core tables verified cleanly: <strong>Revenue in Depth, Access in Depth, System Event in Depth, Parking Transactions</strong>.</p>
        <p class="text-[11px] text-emerald-300/80">Delivered &amp; verified in SharePoint folder: <strong>${escapeHtml(targetFolder)}</strong>.</p>
      `;
    }
  } else if (job.status === "failed") {
    banner.classList.remove("hidden");
    const blocked = Boolean(report && report.upload_blocked);
    const hasPartial = Object.values(job.table_details || report?.tables || {}).some(t => t && t.status === "partial");
    banner.className = "p-4 rounded-xl border text-xs bg-rose-500/15 border-rose-500/40 text-rose-200 space-y-1";
    banner.innerHTML = `
      <div class="flex items-center gap-2 font-bold text-rose-300 text-sm">
        <i class="fa-solid ${blocked ? "fa-ban" : "fa-circle-xmark"}"></i>
        <span>${blocked ? "SharePoint Upload Blocked by Data Validation Gate" : "Workflow Halted / Caught Exception"}</span>
      </div>
      ${hasPartial ? '<p class="text-amber-300 font-semibold">Partial data detected — unsafe upload prevented to protect downstream reports.</p>' : ""}
      <p class="font-mono">${escapeHtml(job.error || report?.summary || "Extraction did not complete cleanly.")}</p>
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
