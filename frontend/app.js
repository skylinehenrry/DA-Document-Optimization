/*
Tools for driving the DA Document Generator frontend.

This file is responsible for:
- Reading analysis settings from the browser form
- Calling the local Python backend for folder browsing and analysis
- Updating output links after files are generated
- Sending heartbeat pings so the backend can close when the UI closes
- Showing lightweight status messages to the user
*/

const form = document.getElementById("analysisForm");
const runButton = document.getElementById("runButton");
const statusToast = document.getElementById("statusToast");
const runLogPanel = document.getElementById("runLogPanel");
const runLogStatus = document.getElementById("runLogStatus");
const runButtonText = document.getElementById("runButtonText");
const elapsedTimer = document.getElementById("elapsedTimer");
const elapsedTimeValue = document.getElementById("elapsedTimeValue");
const progressTrackFill = document.getElementById("progressTrackFill");
const progressStageLabel = document.getElementById("progressStageLabel");
const progressSteps = Array.from(document.querySelectorAll(".progress-step"));
const customDropdowns = Array.from(document.querySelectorAll("[data-custom-dropdown]"));
let logPoller = null;
let elapsedTimerInterval = null;
let runStartedAt = null;

const progressStages = [
  {
    label: "Paths Received",
    patterns: ["Script folder received", "DA Document folder received"],
  },
  {
    label: "Scripts Found",
    patterns: ["Found ", "supported script file"],
  },
  {
    label: "Dependencies",
    patterns: ["Extracting imports", "Starting dependency extraction", "Dependency profile extraction complete"],
  },
  {
    label: "Workflow",
    patterns: ["Constructing workflow dependency network", "Workflow dependency network complete"],
  },
  {
    label: "Summaries",
    patterns: ["Generating script summaries", "Script summary generation complete"],
  },
  {
    label: "Outputs",
    patterns: ["Saving JSON outputs", "Rendering workflow flowchart", "Analysis complete"],
  },
];

const outputReadyCopy = {
  da_document: "Open the generated DA document.",
  flowchart: "View the generated application flowchart.",
  profiles: "Browse the extracted profiles in JSON format.",
  network: "Explore the workflow connection network.",
  summaries: "Review the analysis summaries and metrics.",
};

const outputNotReadyCopy = {
  da_document: "DA document is not ready yet.",
  flowchart: "Flowchart file is not ready yet.",
  profiles: "Profiles JSON is not ready yet.",
  network: "Workflow network is not ready yet.",
  summaries: "Summaries file is not ready yet.",
};

function heartbeatEnabled() {
  // Heartbeat only works when the page is served by the local Python backend.
  return window.location.protocol !== "file:";
}

function sendHeartbeat() {
  // Tell the backend that the frontend is still open.
  if (!heartbeatEnabled()) {
    return;
  }

  fetch("/api/heartbeat", { method: "POST" }).catch(() => {});
}

function sendFinalHeartbeat() {
  // sendBeacon is useful during page close because it does not block navigation.
  if (!heartbeatEnabled() || !navigator.sendBeacon) {
    return;
  }

  navigator.sendBeacon("/api/heartbeat");
}

function setStatus(message, state = "ready") {
  // Display a compact status message without interrupting the user's workflow.
  statusToast.textContent = message;
  statusToast.hidden = false;
  statusToast.dataset.state = state;
}

function formatElapsedTime(milliseconds) {
  // Convert elapsed milliseconds into HH:MM:SS for the run timer.
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = String(Math.floor(totalSeconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

function updateElapsedTimer() {
  // Refresh the elapsed timer while the backend workflow is running.
  if (runStartedAt === null) {
    elapsedTimeValue.textContent = "00:00:00";
    return;
  }

  elapsedTimeValue.textContent = formatElapsedTime(Date.now() - runStartedAt);
}

function startElapsedTimer() {
  // Start the visible timer that sits below the Run button.
  stopElapsedTimer(false);
  runStartedAt = Date.now();
  elapsedTimer.hidden = false;
  updateElapsedTimer();
  elapsedTimerInterval = window.setInterval(updateElapsedTimer, 1000);
}

function stopElapsedTimer(hideTimer = false) {
  // Stop the timer while optionally keeping the final elapsed value visible.
  if (elapsedTimerInterval !== null) {
    window.clearInterval(elapsedTimerInterval);
    elapsedTimerInterval = null;
  }

  updateElapsedTimer();
  runStartedAt = null;

  if (hideTimer) {
    elapsedTimer.hidden = true;
    elapsedTimeValue.textContent = "00:00:00";
  }
}

function setRunButtonState(isRunning) {
  // Swap the Run button between the idle action and running status.
  runButton.disabled = isRunning;
  runButton.classList.toggle("is-running", isRunning);
  runButtonText.textContent = isRunning ? "Running..." : "Run Analysis";
}

function setProgressStage(stageIndex, state = "running") {
  // Update the progress bar and stage chips using one zero-based stage index.
  const clampedIndex = Math.min(Math.max(stageIndex, 0), progressStages.length - 1);
  const isComplete = state === "complete";
  const isError = state === "error";
  const progressIndex = isComplete ? progressStages.length : clampedIndex;
  const progressPercent = (progressIndex / progressStages.length) * 100;

  progressTrackFill.style.width = `${progressPercent}%`;
  progressStageLabel.textContent = isComplete ? "Complete" : progressStages[clampedIndex].label;

  progressSteps.forEach((step, index) => {
    step.classList.toggle("is-complete", isComplete ? true : index < clampedIndex);
    step.classList.toggle("is-current", !isComplete && !isError && index === clampedIndex);
    step.classList.toggle("is-error", isError && index === clampedIndex);
  });
}

function resetProgress() {
  // Return the progress bar to the waiting state before a new run starts.
  progressTrackFill.style.width = "0%";
  progressStageLabel.textContent = "Waiting";

  progressSteps.forEach((step) => {
    step.classList.remove("is-current");
    step.classList.remove("is-complete", "is-error");
  });
}

function getProgressStageFromLogs(logs = []) {
  // Infer the latest workflow stage from the readable backend log messages.
  let latestStageIndex = 0;

  logs.forEach((log) => {
    progressStages.forEach((stage, stageIndex) => {
      if (stage.patterns.some((pattern) => log.includes(pattern))) {
        latestStageIndex = Math.max(latestStageIndex, stageIndex);
      }
    });
  });

  return latestStageIndex;
}

function updateProgressFromLogs(logs = []) {
  // Keep the progress UI synchronized with the same logs shown to the user.
  if (!logs.length) {
    return;
  }

  const hasFailed = logs.some((log) => log.includes("Analysis failed"));
  const hasCompleted = logs.some((log) => log.includes("Analysis complete"));
  const latestStageIndex = getProgressStageFromLogs(logs);

  if (hasFailed) {
    setProgressStage(latestStageIndex, "error");
    progressStageLabel.textContent = "Needs Attention";
    return;
  }

  if (hasCompleted) {
    setProgressStage(progressStages.length - 1, "complete");
    return;
  }

  setProgressStage(latestStageIndex);
}

function renderLogs(logs = []) {
  // Render backend progress messages in the Run Log panel.
  runLogPanel.innerHTML = "";
  updateProgressFromLogs(logs);

  if (!logs.length) {
    const emptyMessage = document.createElement("div");
    emptyMessage.className = "run-log-empty";
    emptyMessage.textContent = "Run messages will appear here after analysis starts.";
    runLogPanel.appendChild(emptyMessage);
    return;
  }

  logs.forEach((log) => {
    const line = document.createElement("div");
    line.className = "run-log-line";
    line.textContent = log;
    runLogPanel.appendChild(line);
  });

  runLogPanel.scrollTop = runLogPanel.scrollHeight;
}

async function fetchLogs() {
  // Poll the backend for the latest progress messages.
  if (window.location.protocol === "file:") {
    return;
  }

  try {
    const response = await fetch("/api/logs");

    if (!response.ok) {
      return;
    }

    const result = await response.json();
    renderLogs(result.logs || []);
  } catch (error) {
    // Log polling should never interrupt the main workflow UI.
  }
}

function startLogPolling() {
  // Start periodic log refresh while analysis is running.
  if (logPoller !== null) {
    window.clearInterval(logPoller);
    logPoller = null;
  }

  runLogStatus.textContent = "Running";
  runLogStatus.dataset.state = "running";
  fetchLogs();
  logPoller = window.setInterval(fetchLogs, 1500);
}

function stopLogPolling(state = "complete") {
  // Stop periodic log refresh after analysis finishes or fails.
  if (logPoller !== null) {
    window.clearInterval(logPoller);
    logPoller = null;
  }

  runLogStatus.textContent = state === "error" ? "Error" : "Complete";
  runLogStatus.dataset.state = state;
}

function getDropdownButton(dropdown) {
  // Each custom dropdown has one visible button that opens the option list.
  return dropdown.querySelector(".model-dropdown-button");
}

function getDropdownOptions(dropdown) {
  // Scope option lookup to one dropdown so Model and Language do not affect each other.
  return Array.from(dropdown.querySelectorAll(".model-dropdown-option"));
}

function closeDropdown(dropdown) {
  // Close one custom picker without changing the selected value.
  const button = getDropdownButton(dropdown);
  dropdown.classList.remove("is-open");

  if (button) {
    button.setAttribute("aria-expanded", "false");
  }
}

function closeAllDropdowns(exceptDropdown = null) {
  // Keep only one custom dropdown open at a time.
  customDropdowns.forEach((dropdown) => {
    if (dropdown !== exceptDropdown) {
      closeDropdown(dropdown);
    }
  });
}

function openDropdown(dropdown) {
  // Open the menu from a fixed top edge instead of using the browser native picker.
  const button = getDropdownButton(dropdown);
  closeAllDropdowns(dropdown);
  dropdown.classList.add("is-open");

  if (button) {
    button.setAttribute("aria-expanded", "true");
  }
}

function toggleDropdown(dropdown) {
  // Toggle whichever picker the user clicked, such as Model or Language.
  if (dropdown.classList.contains("is-open")) {
    closeDropdown(dropdown);
    return;
  }

  openDropdown(dropdown);
}

function selectDropdownOption(dropdown, option) {
  // Store the selected option in the hidden input used by the backend payload.
  const input = dropdown.querySelector('input[type="hidden"]');
  const valueLabel = getDropdownButton(dropdown).querySelector("span:first-child");

  input.value = option.dataset.value;
  valueLabel.textContent = option.textContent.trim();

  getDropdownOptions(dropdown).forEach((dropdownOption) => {
    const isSelected = dropdownOption === option;
    dropdownOption.classList.toggle("is-selected", isSelected);
    dropdownOption.setAttribute("aria-selected", String(isSelected));
  });

  closeDropdown(dropdown);
  getDropdownButton(dropdown).focus();
}

function moveDropdownSelection(dropdown, direction) {
  // Let arrow keys move through the active dropdown's available options.
  const options = getDropdownOptions(dropdown);
  const currentIndex = options.findIndex((option) => option.classList.contains("is-selected"));
  const nextIndex = (currentIndex + direction + options.length) % options.length;
  selectDropdownOption(dropdown, options[nextIndex]);
}

function collectPayload() {
  // Convert form fields into the request shape expected by the backend.
  return {
    script_folder: document.getElementById("scriptFolder").value.trim(),
    da_document_folder: document.getElementById("documentFolder").value.trim(),
    model: document.getElementById("model").value,
    language: document.getElementById("language").value,
    max_concurrency: Number(document.getElementById("maxConcurrency").value),
  };
}

function updateOutputLinks(outputs = {}) {
  // Enable output cards only when the backend confirms the file exists.
  document.querySelectorAll("[data-output-link]").forEach((card) => {
    const key = card.dataset.outputLink;
    const copy = card.querySelector(".output-copy");
    const href = outputs[key];

    if (href) {
      card.href = href;
      card.classList.remove("is-disabled");
      card.setAttribute("aria-label", `Open ${key.replace("_", " ")}`);

      if (copy) {
        copy.textContent = outputReadyCopy[key];
      }
    } else {
      card.href = "#";
      card.classList.add("is-disabled");
      card.setAttribute("aria-label", outputNotReadyCopy[key]);

      if (copy) {
        copy.textContent = outputNotReadyCopy[key];
      }
    }
  });
}

async function refreshOutputStatus() {
  // Ask the backend which files already exist in the selected output folder.
  if (window.location.protocol === "file:") {
    updateOutputLinks({});
    return;
  }

  const documentFolder = document.getElementById("documentFolder").value.trim();

  if (!documentFolder) {
    updateOutputLinks({});
    return;
  }

  try {
    const response = await fetch("/api/output-status", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        da_document_folder: documentFolder,
      }),
    });

    if (!response.ok) {
      updateOutputLinks({});
      return;
    }

    const result = await response.json();
    updateOutputLinks(result.outputs || {});
  } catch (error) {
    updateOutputLinks({});
  }
}

async function browseFolder(targetInputId) {
  // Ask the backend to open a native folder picker and fill the matching input.
  const input = document.getElementById(targetInputId);

  if (window.location.protocol === "file:") {
    setStatus("Open http://127.0.0.1:8000 to use the folder browse dialog.", "error");
    return;
  }

  try {
    const response = await fetch("/api/browse-folder", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        target: targetInputId,
        current_path: input.value.trim(),
      }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `Backend returned ${response.status}`);
    }

    const result = await response.json();

    if (result.path) {
      input.value = result.path;
      input.focus();

      if (targetInputId === "documentFolder") {
        refreshOutputStatus();
      }
    }
  } catch (error) {
    setStatus("Folder browse requires the local Python backend at http://127.0.0.1:8000.", "error");
  }
}

form.addEventListener("submit", async (event) => {
  // Run the analysis workflow through the backend once the endpoint is connected.
  event.preventDefault();

  const payload = collectPayload();
  setRunButtonState(true);
  startElapsedTimer();
  resetProgress();
  setStatus("Running analysis...", "running");
  renderLogs([]);
  updateOutputLinks({});
  startLogPolling();

  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`Backend returned ${response.status}`);
    }

    const result = await response.json();
    updateOutputLinks(result.outputs);
    setStatus(result.message || "Analysis complete.");
    await fetchLogs();
    stopLogPolling("complete");
    setProgressStage(progressStages.length - 1, "complete");
  } catch (error) {
    setStatus(error.message || "Unable to run analysis.", "error");
    await fetchLogs();
    stopLogPolling("error");
  } finally {
    stopElapsedTimer();
    setRunButtonState(false);
  }
});

document.querySelectorAll("[data-folder-target]").forEach((button) => {
  // Connect each Browse button to its corresponding path input.
  button.addEventListener("click", () => {
    browseFolder(button.dataset.folderTarget);
  });
});

customDropdowns.forEach((dropdown) => {
  // Reuse the same dropdown behavior for Model, Language, and future pickers.
  const button = getDropdownButton(dropdown);

  button.addEventListener("click", () => {
    toggleDropdown(dropdown);
  });

  getDropdownOptions(dropdown).forEach((option) => {
    option.addEventListener("click", () => {
      selectDropdownOption(dropdown, option);
    });
  });

  dropdown.addEventListener("keydown", (event) => {
    // Provide the expected keyboard behavior for the custom dropdown.
    if (event.key === "Escape") {
      closeDropdown(dropdown);
      button.focus();
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      openDropdown(dropdown);
      moveDropdownSelection(dropdown, 1);
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      openDropdown(dropdown);
      moveDropdownSelection(dropdown, -1);
    }
  });
});

document.getElementById("documentFolder").addEventListener("change", refreshOutputStatus);
document.getElementById("documentFolder").addEventListener("blur", refreshOutputStatus);

document.addEventListener("click", (event) => {
  // Close open menus when the user clicks anywhere outside all custom dropdowns.
  const clickedInsideDropdown = customDropdowns.some((dropdown) => dropdown.contains(event.target));

  if (!clickedInsideDropdown) {
    closeAllDropdowns();
  }
});

sendHeartbeat();
setInterval(sendHeartbeat, 5000);
window.addEventListener("pagehide", sendFinalHeartbeat);

refreshOutputStatus();
