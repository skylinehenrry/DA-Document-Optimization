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
const modelDropdown = document.querySelector("[data-model-dropdown]");
const modelDropdownButton = document.getElementById("modelDropdownButton");
const modelDropdownValue = document.getElementById("modelDropdownValue");
const modelInput = document.getElementById("model");
let logPoller = null;

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

function renderLogs(logs = []) {
  // Render backend progress messages in the Run Log panel.
  runLogPanel.innerHTML = "";

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
  stopLogPolling();
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

function closeModelDropdown() {
  // Close the custom model picker without changing the selected model.
  modelDropdown.classList.remove("is-open");
  modelDropdownButton.setAttribute("aria-expanded", "false");
}

function openModelDropdown() {
  // Open the menu from a fixed top edge instead of using the macOS native picker.
  modelDropdown.classList.add("is-open");
  modelDropdownButton.setAttribute("aria-expanded", "true");
}

function toggleModelDropdown() {
  // Toggle the menu when the user clicks the model control.
  if (modelDropdown.classList.contains("is-open")) {
    closeModelDropdown();
    return;
  }

  openModelDropdown();
}

function selectModelOption(option) {
  // Store the selected model in the hidden input used by the backend payload.
  modelInput.value = option.dataset.value;
  modelDropdownValue.textContent = option.textContent.trim();

  document.querySelectorAll(".model-dropdown-option").forEach((modelOption) => {
    const isSelected = modelOption === option;
    modelOption.classList.toggle("is-selected", isSelected);
    modelOption.setAttribute("aria-selected", String(isSelected));
  });

  closeModelDropdown();
  modelDropdownButton.focus();
}

function moveModelSelection(direction) {
  // Let arrow keys move through available model options.
  const options = Array.from(document.querySelectorAll(".model-dropdown-option"));
  const currentIndex = options.findIndex((option) => option.classList.contains("is-selected"));
  const nextIndex = (currentIndex + direction + options.length) % options.length;
  selectModelOption(options[nextIndex]);
}

function collectPayload() {
  // Convert form fields into the request shape expected by the backend.
  return {
    script_folder: document.getElementById("scriptFolder").value.trim(),
    da_document_folder: document.getElementById("documentFolder").value.trim(),
    model: modelInput.value,
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
  runButton.disabled = true;
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
  } catch (error) {
    setStatus(error.message || "Unable to run analysis.", "error");
    await fetchLogs();
    stopLogPolling("error");
  } finally {
    runButton.disabled = false;
  }
});

document.querySelectorAll("[data-folder-target]").forEach((button) => {
  // Connect each Browse button to its corresponding path input.
  button.addEventListener("click", () => {
    browseFolder(button.dataset.folderTarget);
  });
});

document.getElementById("documentFolder").addEventListener("change", refreshOutputStatus);
document.getElementById("documentFolder").addEventListener("blur", refreshOutputStatus);

modelDropdownButton.addEventListener("click", toggleModelDropdown);

document.querySelectorAll(".model-dropdown-option").forEach((option) => {
  option.addEventListener("click", () => {
    selectModelOption(option);
  });
});

document.addEventListener("click", (event) => {
  // Close the menu when the user clicks anywhere outside the custom dropdown.
  if (!modelDropdown.contains(event.target)) {
    closeModelDropdown();
  }
});

modelDropdown.addEventListener("keydown", (event) => {
  // Provide the expected keyboard behavior for the custom dropdown.
  if (event.key === "Escape") {
    closeModelDropdown();
    modelDropdownButton.focus();
  }

  if (event.key === "ArrowDown") {
    event.preventDefault();
    openModelDropdown();
    moveModelSelection(1);
  }

  if (event.key === "ArrowUp") {
    event.preventDefault();
    openModelDropdown();
    moveModelSelection(-1);
  }
});

sendHeartbeat();
setInterval(sendHeartbeat, 5000);
window.addEventListener("pagehide", sendFinalHeartbeat);

refreshOutputStatus();
