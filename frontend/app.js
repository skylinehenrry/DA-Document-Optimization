const form = document.getElementById("analysisForm");
const runButton = document.getElementById("runButton");
const statusToast = document.getElementById("statusToast");

const outputBase =
  window.location.protocol === "file:" ? "../backend/outputs" : "/outputs";

const outputLinks = {
  da_document: `${outputBase}/DA_Document.docx`,
  flowchart: `${outputBase}/workflow_flowchart.html`,
  profiles: `${outputBase}/profiles.json`,
  network: `${outputBase}/workflow_network.json`,
  summaries: `${outputBase}/summaries.json`,
};

function heartbeatEnabled() {
  return window.location.protocol !== "file:";
}

function sendHeartbeat() {
  if (!heartbeatEnabled()) {
    return;
  }

  fetch("/api/heartbeat", { method: "POST" }).catch(() => {});
}

function sendFinalHeartbeat() {
  if (!heartbeatEnabled() || !navigator.sendBeacon) {
    return;
  }

  navigator.sendBeacon("/api/heartbeat");
}

function setStatus(message, state = "ready") {
  statusToast.textContent = message;
  statusToast.hidden = false;
  statusToast.dataset.state = state;
}

function collectPayload() {
  return {
    script_folder: document.getElementById("scriptFolder").value.trim(),
    da_document_folder: document.getElementById("documentFolder").value.trim(),
    model: document.getElementById("model").value,
    max_concurrency: Number(document.getElementById("maxConcurrency").value),
  };
}

function updateOutputLinks(outputs = {}) {
  document.querySelectorAll("[data-output-link]").forEach((card) => {
    const key = card.dataset.outputLink;
    const href = outputs[key] || outputLinks[key];

    if (href) {
      card.href = href;
      card.classList.remove("is-disabled");
    }
  });
}

async function browseFolder(targetInputId) {
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
    }
  } catch (error) {
    setStatus("Folder browse requires the local Python backend at http://127.0.0.1:8000.", "error");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const payload = collectPayload();
  runButton.disabled = true;
  setStatus("Running analysis...", "running");

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
    setStatus("Analysis complete.");
  } catch (error) {
    setStatus(error.message || "Unable to run analysis.", "error");
  } finally {
    runButton.disabled = false;
  }
});

document.querySelectorAll("[data-folder-target]").forEach((button) => {
  button.addEventListener("click", () => {
    browseFolder(button.dataset.folderTarget);
  });
});

sendHeartbeat();
setInterval(sendHeartbeat, 5000);
window.addEventListener("pagehide", sendFinalHeartbeat);

updateOutputLinks();
