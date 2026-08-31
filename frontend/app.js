/*
Application controller for DA Document Generator.

- Load the durable workflow library and resume the selected draft after reload.
- Submit analysis, edits, imports and generation as persistent background jobs.
- Keep the exact request ID before submission, so a lost response cannot start
  duplicate analysis or duplicate paid model requests when connectivity returns.
- Preserve unsaved graph changes separately from the authoritative server graph.
- Require explicit resolution of stale revisions and explicit retry of interrupted
  jobs; never treat a browser disconnect as proof that an operation failed.
- Render source evidence as text, and use API artifact URLs rather than local paths.
*/

import {APIError, PendingRequest, RecoveryStorage, artifactURL, downloadFilename, request} from "./api.js";
import {EDGE_KINDS, NODE_KINDS, GraphSession, newId, validateConnection} from "./graph-state.js";
import {GraphEditor} from "./graph-editor.js";
import {basename, connectionGroups, nodeName} from "./graph-presentation.js";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const readable = value => String(value ?? "").replaceAll("_", " ");
const jobNames = {analyze: "Project analysis", edit: "Save diagram changes", import: "Import corrected diagram", generate: "Flowchart generation", suggest: "AI connection suggestions"};
const activeStates = new Set(["queued", "running"]);
const storage = new RecoveryStorage();
const pending = new PendingRequest(storage);
const storedPreferences = storage.read("preferences", {});
const preferences = storedPreferences && typeof storedPreferences === "object" && !Array.isArray(storedPreferences) ? storedPreferences : {};
const storedLibrary = storage.read("library", []);
const formFields = ["workflowName", "scriptFolder", "documentFolder", "workingDirectory", "sqlDialect", "databaseNamespace"];
const optionFields = ["model", "language", "maxConcurrency", "timeoutSeconds"];
const state = {
  online: false, health: null, step: ["analyze", "review", "generate"].includes(preferences.step) ? preferences.step : "analyze", tab: "diagram",
  drafts: Array.isArray(storedLibrary) ? storedLibrary.filter(row => row && typeof row === "object" && draftId(row)) : [], jobs: [], selected: null, session: null,
  selectedId: preferences.selectedId ?? null, watchJobId: preferences.watchJobId ?? null,
  handled: new Set(Array.isArray(preferences.handled) ? preferences.handled : []), selection: null, polling: false,
  lastLibrary: 0, loadToken: 0, storageUnsafe: false, expandedGroups: new Set(),
  editorAction: null, stopping: false, optionsForDraft: null,
};

function on(id, event, handler) {
  $(id).addEventListener(event, async value => {
    try { await handler(value); } catch (error) { showError(error); }
  });
}
/*
Apply the user's accent theme without touching workflow data.
- Accept only the three named palettes defined in styles.css.
- Keep this preference separate from recovery requests and draft revisions.
- Update the native radio controls and status text as well as the visible colours.
- If storage is blocked, retain the current appearance and explain that it is
  temporary; a cosmetic preference must never block analysis or saving.
*/
function applyAppearance(value, {persist = false} = {}) {
  const themes = {blue: "Blue", violet: "Violet", graphite: "Graphite"};
  const theme = typeof value === "string" && Object.hasOwn(themes, value) ? value : "blue";
  document.documentElement.dataset.theme = theme;
  document.querySelectorAll('input[name="accentTheme"]').forEach(input => {
    input.checked = input.value === theme;
  });
  $("themeStatus").textContent = `${themes[theme]} theme selected.`;
  if (persist) {
    try { storage.write("appearance.theme", theme); }
    catch { $("themeStatus").textContent = `${themes[theme]} is active for this page. Browser storage is unavailable, so this choice cannot be saved.`; }
  }
}

/*
Keep the saved-workflow drawer's visible and accessible states together.
- The drawer is navigation rather than a modal; normal page controls still work.
- CSS hides closed navigation from sight and keyboard focus on every screen size.
- Closing with Escape or the close button returns focus to the menu button.
*/
function setLibraryOpen(open, {returnFocus = false} = {}) {
  $("librarySidebar").classList.toggle("is-open", open);
  $("toggleLibrary").setAttribute("aria-expanded", String(open));
  if (returnFocus) $("toggleLibrary").focus();
}

function dateText(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}
function draftId(row) { return row?.draft_id ?? row?.id; }
function currentJob() {
  return state.jobs.find(job => job.id === state.watchJobId) ?? state.jobs.find(job => activeStates.has(job.state) && job.draft_id === state.selectedId);
}
function busyDraft() {
  const uncertain = pending.value;
  return Boolean(uncertain && (!uncertain.body.draft_id || uncertain.body.draft_id === state.selectedId)) ||
    state.jobs.some(job => activeStates.has(job.state) && job.draft_id === state.selectedId);
}
function canEdit() { return state.session && !state.session.conflict && !busyDraft(); }
function safeWrite(key, value) {
  try { storage.write(key, value); return true; }
  catch (error) { state.storageUnsafe = true; showMessage(error.message, "warning"); return false; }
}
function savePreferences() {
  preferences.selectedId = state.selectedId;
  preferences.step = state.step;
  preferences.watchJobId = state.watchJobId;
  preferences.handled = [...state.handled].slice(-40);
  preferences.form = Object.fromEntries(formFields.map(id => [id, $(id).value]));
  safeWrite("preferences", preferences);
}
function persistEdits() {
  if (!state.session) return;
  const key = `edits.${state.session.base.id}`;
  if (state.session.dirty || state.session.conflict) safeWrite(key, state.session.recovery());
  else storage.remove(key);
}
function showMessage(message, type = "success") {
  const element = $("appMessage");
  element.className = `notice ${type === "error" ? "notice-error" : type === "warning" ? "notice-warning" : ""}`;
  element.innerHTML = `${icon(type === "success" ? "check" : "info")}<div>${esc(message)}</div><button class="icon-button notice-dismiss" type="button" aria-label="Dismiss message" data-dismiss-message>${icon("close")}</button>`;
  element.hidden = false;
}
function showError(error) {
  showMessage(error.message ?? "Something went wrong. Your saved draft has not been deleted.", error.uncertain ? "warning" : "error");
  renderControls();
}
async function confirmAction(title, description, label = "Continue") {
  $("confirmTitle").textContent = title;
  $("confirmDescription").textContent = description;
  $("confirmAction").textContent = label;
  const dialog = $("confirmDialog");
  dialog.returnValue = "cancel";
  dialog.showModal();
  return await new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true}));
}

const editor = new GraphEditor($("graphCanvas"), {
  onSelect: selection => { state.selection = selection; renderInspector(); },
  onMove: (id, position) => editGraph(graph => { graph.nodes.find(node => node.id === id).position = position; }),
  onDelete: selection => deleteSelection(selection).catch(showError),
  onViewChange: view => {
    $("zoomLevel").textContent = `${view.zoom}%`;
    const total = state.session?.graph.nodes.length ?? 0;
    $("canvasVisibleCount").textContent = `${view.nodes} of ${total} nodes · ${view.edges} visible arrows`;
    $("emptyCanvas").hidden = view.nodes > 0;
  },
});

// - Grouping only changes the number of drawn arrows. Switching to the full
//   relationship view never changes, adds or removes any saved edge records.
const relationshipView = document.createElement("select");
relationshipView.id = "relationshipView";
relationshipView.setAttribute("aria-label", "Connection display");
relationshipView.innerHTML = '<option value="direct">Direct connections</option><option value="all">All relationships</option>';
$("canvasScope").insertAdjacentElement("afterend", relationshipView);
const exportEditsButton = document.createElement("button");
exportEditsButton.id = "exportEditsButton";
exportEditsButton.type = "button";
exportEditsButton.className = "text-button";
exportEditsButton.dataset.downloadRecovery = "true";
exportEditsButton.innerHTML = `${icon("download")}Download edits`;
exportEditsButton.title = "Download the unsaved node and connection changes as JSON, without the server";
$("discardButton").insertAdjacentElement("beforebegin", exportEditsButton);

/*
Stage navigation.
- Show one panel at a time without creating another draft or starting a job.
- Keep the active step and keyboard state in sync with the visible content.
- Reapply the canvas viewport after revealing a previously hidden diagram.
*/
function setStep(step) {
  if (step !== "analyze" && !state.selected) return;
  state.step = step;
  document.documentElement.dataset.step = step;
  const stepOrder = ["analyze", "review", "generate"];
  const currentIndex = stepOrder.indexOf(step);
  for (const name of ["analyze", "review", "generate"]) $(name + "Panel").hidden = name !== step;
  document.querySelectorAll("[data-step]").forEach(button => {
    const current = button.dataset.step === step;
    button.classList.toggle("is-current", current);
    button.classList.toggle("is-complete", stepOrder.indexOf(button.dataset.step) < currentIndex || (button.dataset.step === "generate" && Boolean(state.selected?.generation)));
    if (current) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current");
    button.disabled = button.dataset.step !== "analyze" && !state.selected;
  });
  if (step === "review") requestAnimationFrame(() => editor.applyView());
  if (step === "generate") renderGeneration();
  renderProgress();
  renderRunLog();
  savePreferences();
}

/*
Keep the historical Current Progress treatment aligned with durable state.

- The line advances by saved workflow stages instead of pretending that individual
  parsers or model calls have a precise completion percentage.
- An active durable job takes priority in the status badge, even if the browser was
  reopened on another stage while that operation continued in the background.
- Completion styling is paired with text and icons, so colour is never the only cue.
*/
function renderProgress() {
  const job = currentJob();
  const generated = Boolean(state.selected?.generation && artifactURL(state.selected?.outputs?.flowchart));
  const widths = {analyze: state.selected ? 34 : 4, review: 67, generate: generated ? 100 : 84};
  let label = state.step === "analyze" ? "Ready" : state.step === "review" ? "Workflow review" : generated ? "Outputs ready" : "Ready to generate";
  let width = widths[state.step] ?? 4;
  if (job && activeStates.has(job.state)) {
    const labels = {analyze: "Analyzing", edit: "Saving review", import: "Importing review", suggest: "Finding suggestions", generate: "Generating outputs"};
    label = labels[job.kind] ?? "Processing";
    if (job.kind === "analyze") width = Math.max(width, 18);
    if (["edit", "import", "suggest"].includes(job.kind)) width = Math.max(width, 55);
    if (job.kind === "generate") width = Math.max(width, 84);
  }
  $("progressStageLabel").textContent = label;
  $("progressStageLabel").className = `badge ${job && activeStates.has(job.state) ? "" : "info"}`;
  $("progressTrackFill").style.width = `${width}%`;
}
function setReviewTab(tab) {
  state.tab = tab;
  for (const name of ["diagram", "findings", "sources"]) {
    $(name + "View").hidden = name !== tab;
    const button = $("tab-" + name);
    button.classList.toggle("is-current", name === tab);
    button.setAttribute("aria-selected", String(name === tab));
    button.tabIndex = name === tab ? 0 : -1;
  }
  if (tab === "diagram") requestAnimationFrame(() => editor.applyView());
  if (tab === "findings") renderFindings();
  if (tab === "sources") renderSources();
}
function renderLibrary() {
  const query = $("librarySearch").value.toLowerCase();
  const drafts = state.drafts.filter(row => `${row.title ?? ""} ${row.project_root ?? ""}`.toLowerCase().includes(query));
  $("draftList").innerHTML = drafts.length ? drafts.map(row => `<button type="button" class="draft-item ${draftId(row) === state.selectedId ? "is-selected" : ""}" data-draft="${esc(draftId(row))}" ${draftId(row) === state.selectedId ? 'aria-current="true"' : ""} title="${esc(row.project_root)}"><span class="draft-item-title">${icon("flow")}<span>${esc(row.title || basename(row.project_root) || "Untitled workflow")}</span></span><span class="draft-item-meta">r${Number(row.revision) || 1} · ${row.status === "generated" ? "Generated" : "Draft"} · ${Number(row.source_count) || 0} files</span></button>`).join("") : `<p class="library-empty">${query ? "No workflows match this search." : "Your saved workflows will appear here. Start with New analysis."}</p>`;
}

function loadOptions(result) {
  if (state.optionsForDraft === result.draft_id) return;
  state.optionsForDraft = result.draft_id;
  const saved = preferences.options?.[result.draft_id] ?? {};
  const previous = result.generation?.settings ?? {};
  const settings = {...(result.settings ?? {}), ...previous, ...saved};
  $("model").value = settings.model ?? "OpenAI";
  $("language").value = settings.language === "Japanese" ? "Japanese" : "English";
  $("maxConcurrency").value = settings.max_concurrency ?? (settings.model === "Ollama" ? 1 : 3);
  $("timeoutSeconds").value = settings.timeout_seconds ?? 90;
  // - Reopening a historical AI generation does not opt the user into a new
  //   provider call. Only a saved browser choice restores the checked option.
  $("useLlmSummaries").checked = saved.use_llm ?? false;
  $("allowProposedEdges").checked = false;
  $("acknowledgeIncomplete").checked = false;
}
/*
Reconcile a saved server draft with any browser recovery copy.
- The server revision remains authoritative; local edits are a separate session.
- Match completed submission IDs before interpreting a revision change as a conflict.
- Never discard unsubmitted edits just because the page or backend restarted.
*/
function takeDraft(result, {fit = false, step = null, completedRequest = null} = {}) {
  if (!result?.graph) throw new Error("The saved draft response did not include a graph.");
  const previous = state.session;
  const same = previous?.base.id === result.draft_id;
  let recovered = same && (previous.dirty || previous.conflict) ? previous.recovery() : storage.read(`edits.${result.draft_id}`);
  // - If the page closed between accepting a job and saving its UI selection,
  //   recognize its durable request ID from job history before declaring a
  //   stale-edit conflict against the revision that very job already saved.
  const savedEdit = recovered?.submitted_request_id && state.jobs.find(job => job.kind === "edit" && job.state === "succeeded" &&
    job.request_id === recovered.submitted_request_id && (job.draft_id ?? job.result?.draft_id) === result.draft_id);
  if (savedEdit) completedRequest = savedEdit.request_id;
  if (completedRequest && recovered?.submitted_request_id === completedRequest) {
    recovered = null;
    storage.remove(`edits.${result.draft_id}`);
  }
  const revisionChanged = previous?.base.revision !== result.revision || !same;
  state.selected = result;
  state.selectedId = result.draft_id;
  if (!same || revisionChanged || completedRequest) {
    state.session = new GraphSession(result.graph, recovered);
    state.selection = null;
    editor.selection = null;
    $("allowProposedEdges").checked = false;
    $("acknowledgeIncomplete").checked = false;
  }
  if (!state.session) state.session = new GraphSession(result.graph, recovered);
  loadOptions(result);
  safeWrite("last-draft", result);
  $("projectTitle").textContent = result.graph.title;
  $("projectSubtitle").textContent = result.graph.project_root;
  $("breadcrumbTitle").textContent = result.graph.title;
  $("revisionPill").hidden = false;
  $("revisionPill").textContent = `Revision ${result.revision} · ${result.status === "generated" ? "Generated" : "Saved draft"}`;
  renderReview();
  editor.setGraph(state.session.graph, {fit: fit || !same});
  renderInspector();
  renderLibrary();
  setStep(step ?? (state.step === "analyze" ? "review" : state.step));
  if (result.export_warning) showMessage(result.export_warning, "warning");
}
async function loadDraft(id, {step = "review", quiet = false} = {}) {
  const token = ++state.loadToken;
  const result = await request(`/api/drafts/${encodeURIComponent(id)}`);
  if (token !== state.loadToken) return;
  takeDraft(result, {fit: id !== state.selectedId, step});
  if (!quiet) $("appMessage").hidden = true;
  setLibraryOpen(false);
}
function newAnalysis() {
  persistEdits();
  ++state.loadToken;
  state.selected = null; state.session = null; state.selectedId = null; state.selection = null;
  state.optionsForDraft = null;
  $("projectTitle").textContent = "Create a workflow.";
  $("projectSubtitle").textContent = "Understand your project. Refine the connections. Share the result.";
  $("breadcrumbTitle").textContent = "New analysis";
  $("revisionPill").hidden = true;
  $("appMessage").hidden = true;
  setStep("analyze"); renderLibrary(); renderControls();
  setLibraryOpen(false);
  $("workflowName").focus();
}

function renderReview() {
  if (!state.session) return;
  const graph = state.session.graph;
  const review = state.selected.review ?? {};
  const diagnostics = review.diagnostics;
  const partial = graph.sources.filter(source => source.status === "partial").length;
  const failed = graph.sources.filter(source => source.status === "failed").length;
  $("coverageSummary").textContent = diagnostics?.summary ?? `${graph.sources.length} source files analyzed. ${partial} need dependency review; ${failed} could not be fully analyzed.`;
  $("sourceCount").textContent = graph.sources.length;
  $("nodeCount").textContent = graph.nodes.length;
  $("edgeCount").textContent = graph.edges.length;
  $("findingsCount").textContent = (review.issues?.length ?? 0) + graph.edges.filter(edge => edge.status === "proposed").length;
  $("revisionConflict").hidden = !state.session.conflict;
  if (state.session.conflict) $("conflictMessage").textContent = `Browser edits belong to revision ${state.session.conflict.base_revision}; the server is on revision ${state.session.base.revision}. Download the edits for reference, or discard them and use the saved revision. They will not be merged automatically.`;
  renderControls();
  if (state.tab === "findings") renderFindings();
  if (state.tab === "sources") renderSources();
  if (state.step === "generate") renderGeneration();
}
/*
Enable actions from the current recovery, revision and job state.
- Styling must not be used as the only guard against an invalid operation.
- Keep offline inspection/export available while disabling new server requests.
- Never enable generation while unsaved graph changes or a conflict remain.
*/
function renderControls() {
  const session = state.session;
  const busy = busyDraft();
  const locked = !session || Boolean(session.conflict) || busy;
  editor.setDisabled(locked);
  const dirty = Boolean(session?.dirty);
  const changes = session?.changes.length ?? 0;
  $("saveState").classList.toggle("is-dirty", dirty || Boolean(session?.conflict));
  $("saveState").innerHTML = `<i></i>${session?.conflict ? "Revision conflict · edits retained" : busy ? "Waiting for the saved job result…" : dirty ? `${changes} unsaved change${changes === 1 ? "" : "s"} · kept in this browser` : "All changes saved"}`;
  $("undoButton").disabled = locked || !session?.undoStack.length;
  $("redoButton").disabled = locked || !session?.redoStack.length;
  $("discardButton").disabled = busy || (!dirty && !session?.conflict);
  $("exportEditsButton").hidden = !dirty && !session?.conflict;
  $("exportEditsButton").disabled = !session;
  $("saveChangesButton").disabled = locked || !dirty || !state.online || changes > 1000;
  $("addNodeButton").disabled = locked;
  $("addEdgeButton").disabled = locked || !session?.graph.nodes.length;
  $("importDrawioButton").disabled = locked || dirty || !state.online;
  $("downloadDrawioButton").disabled = !session || dirty || !state.online || Boolean(session.conflict);
  $("downloadDrawioButton").title = dirty ? "Save changes before exporting the reviewed draft" : "Download the saved draft for draw.io";
  $("suggestButton").disabled = locked || dirty || !state.online;
  $("goGenerateButton").disabled = !session;
  $("analyzeButton").disabled = !state.online || Boolean(pending.value) || state.jobs.some(job => job.kind === "analyze" && activeStates.has(job.state));
  for (const control of $("inspector").querySelectorAll("input,select,textarea,button[data-mutates]")) control.disabled = locked;
  const proposals = session?.graph.edges.filter(edge => edge.status === "proposed").length ?? 0;
  const needsAcceptance = Boolean(state.selected?.review?.has_analysis_errors);
  $("generateButton").disabled = locked || dirty || !state.online || !session?.graph.nodes.length || (proposals > 0 && !$("allowProposedEdges").checked) || (needsAcceptance && !$("acknowledgeIncomplete").checked);
  const anyActive = state.jobs.some(job => activeStates.has(job.state));
  $("stopServerButton").disabled = !state.online || !state.health?.shutdown_available || anyActive || Boolean(pending.value) || state.stopping;
  document.querySelectorAll("[data-retry-job]").forEach(button => { button.disabled = !state.online || Boolean(pending.value); });
}

function editGraph(update) {
  if (!canEdit()) return false;
  try {
    if (!state.session.change(update)) return false;
    persistEdits();
    if (state.selection && !state.session.graph[state.selection.type === "node" ? "nodes" : "edges"].some(item => item.id === state.selection.id)) state.selection = null;
    editor.selection = state.selection;
    editor.setGraph(state.session.graph);
    renderReview(); renderInspector();
    return true;
  } catch (error) { showError(error); return false; }
}
function selectItem(selection, {focus = false} = {}) {
  state.selection = selection;
  setReviewTab("diagram");
  editor.setSelection(selection);
  if (focus && selection.type === "node") {
    $("canvasSearch").value = "";
    editor.focusNode(selection.id);
  }
  renderInspector();
}
function nodeOptions(selected = "") {
  return state.session.graph.nodes.map(node => `<option value="${esc(node.id)}" ${node.id === selected ? "selected" : ""}>${esc(nodeName(node))}${node.source_path ? ` — ${esc(node.source_path)}` : ""}</option>`).join("");
}
function kindOptions(selected) { return EDGE_KINDS.map(kind => `<option value="${kind}" ${kind === selected ? "selected" : ""}>${esc(readable(kind))}</option>`).join(""); }
function evidenceHTML(items = []) {
  return items.map(item => `<div class="inspector-evidence"><strong>${esc(item.source_path)}${item.line_start ? `:${Number(item.line_start)}` : ""}</strong>${item.excerpt ? `<pre>${esc(item.excerpt)}</pre>` : ""}${item.note ? `<p>${esc(item.note)}</p>` : ""}<small>${esc(item.extractor)}</small></div>`).join("");
}
/*
Present the selected node or connection and its original evidence.
- Short filenames belong on cards; full paths belong in these details.
- Grouped arrows expose their member relationships before any destructive edit.
- Escape all source-supplied content before inserting it into the interface.
*/
function renderInspector() {
  if (!state.session) return;
  const graph = state.session.graph;
  const selection = state.selection;
  const element = $("inspector");
  if (!selection) {
    element.innerHTML = `<div class="inspector-icon">${icon("flow")}</div><p class="eyebrow">THE DETAILS MATTER</p><h3>Select a node or connection</h3><p>Inspect its source evidence, adjust its details, or reconnect an arrow. Changes stay local until you save.</p><label class="field"><span>Jump to a node</span><select id="jumpNode"><option value="">Choose a node…</option>${nodeOptions()}</select></label><div class="inspector-section"><h4>Connection direction</h4><p class="muted">Reads: resource → reader<br>Writes: writer → resource<br>Imports / calls: caller → dependency<br>Workflow wiring: predecessor → successor</p></div>`;
    return;
  }
  if (selection.type === "node") {
    const node = graph.nodes.find(item => item.id === selection.id);
    if (!node) { state.selection = null; renderInspector(); return; }
    const source = graph.sources.find(item => item.path === node.source_path);
    const related = graph.edges.filter(edge => edge.source === node.id || edge.target === node.id);
    element.innerHTML = `<p class="eyebrow">${esc(readable(node.script_type ?? node.kind))} NODE</p><h3>${esc(nodeName(node))}</h3>${node.source_path || node.resource_key ? `<p class="inspector-path">${esc(node.source_path ?? node.resource_key)}</p>` : ""}${source ? `<span class="badge ${source.status === "failed" ? "error" : source.status === "partial" ? "warning" : ""}">${source.status === "failed" ? "Analysis failed" : source.status === "partial" ? "Needs dependency review" : "Analyzed"}</span>` : ""}<form id="nodeInspectorForm"><label class="field"><span>Node label</span><input name="label" value="${esc(node.label)}" maxlength="1000" required>${["script", "file"].includes(node.kind) ? "<small>File cards display filenames only. The full source identity stays unchanged.</small>" : ""}</label><div class="two-columns"><label class="field"><span>X position</span><input name="x" type="number" min="-1000000" max="1000000" step="any" value="${Number(node.position?.x ?? 0)}" required></label><label class="field"><span>Y position</span><input name="y" type="number" min="-1000000" max="1000000" step="any" value="${Number(node.position?.y ?? 0)}" required></label></div><div class="button-row"><button type="submit" class="button button-primary" data-mutates>Apply changes</button><button type="button" class="icon-button" data-remove-node="${esc(node.id)}" data-mutates aria-label="Remove selected node">${icon("trash")}</button></div></form><div class="button-row"><button type="button" class="text-button" data-connect-from="${esc(node.id)}" data-mutates>${icon("plus")}Connect from this node</button></div><div class="inspector-section"><h4>${related.length} saved relationship${related.length === 1 ? "" : "s"}</h4>${related.slice(0, 60).map(edge => { const other = graph.nodes.find(item => item.id === (edge.source === node.id ? edge.target : edge.source)); return `<button type="button" class="inspector-connection" data-select-edge="${esc(edge.id)}">${edge.source === node.id ? "→" : "←"} ${esc(other ? nodeName(other) : "Node")}<span>${esc(readable(edge.kind))}</span></button>`; }).join("")}${related.length > 60 ? '<p class="muted">Use the canvas or All relationships view to inspect the remaining connections.</p>' : ""}</div>`;
  } else {
    const edge = graph.edges.find(item => item.id === selection.id);
    if (!edge) { state.selection = null; renderInspector(); return; }
    const group = connectionGroups(graph).find(item => item.member_ids.includes(edge.id));
    element.innerHTML = `<p class="eyebrow">CONNECTION DETAILS</p><h3>${esc(readable(edge.kind))}</h3><p>${edge.origin === "user" ? "Reviewed or added by you" : edge.origin === "llm" ? "Suggested by the selected model" : "Detected from source evidence"}</p><span class="badge ${edge.status === "proposed" ? "warning" : ""}">${edge.status === "proposed" ? "Unconfirmed suggestion" : "Confirmed"}</span>${editor.grouped && group?.member_ids.length > 1 ? `<div class="inspector-section"><h4>This arrow represents ${group.member_ids.length} relationships</h4><label class="field"><span>Inspect an individual relationship</span><select id="groupMember">${group.member_ids.map(id => { const member = graph.edges.find(item => item.id === id); return `<option value="${esc(id)}" ${id === edge.id ? "selected" : ""}>${esc(readable(member.kind))} · ${member.status === "proposed" ? "unconfirmed" : esc(member.origin)} · ${esc(member.evidence?.[0]?.line_start ?? id.slice(-6))}</option>`; }).join("")}</select></label></div>` : ""}<form id="edgeInspectorForm"><label class="field"><span>From · source</span><select name="source">${nodeOptions(edge.source)}</select></label><label class="field"><span>To · destination</span><select name="target">${nodeOptions(edge.target)}</select></label><label class="field"><span>Connection type</span><select name="kind">${kindOptions(edge.kind)}</select><small>Reads run from a resource to its reader. Writes run from the writer to its resource.</small></label><label class="field"><span>Label · optional</span><input name="label" value="${esc(edge.label)}" maxlength="2000"></label><label class="field"><span>Review note · optional</span><textarea name="review_note">${esc(edge.review_note)}</textarea></label><div class="button-row"><button class="button button-primary" type="submit" data-mutates>Apply changes</button><button class="icon-button" type="button" data-remove-edge="${esc(edge.id)}" data-mutates aria-label="Remove selected connection">${icon("trash")}</button></div></form>${edge.status === "proposed" ? `<div class="button-row"><button class="button" type="button" data-confirm-edge="${esc(edge.id)}" data-mutates>${icon("check")}Confirm connection</button></div>` : ""}<div class="inspector-section"><h4>Source evidence</h4>${edge.evidence?.length ? evidenceHTML(edge.evidence) : '<p class="muted">No source citation for this connection. Manual reconnections keep the original evidence in revision history.</p>'}${edge.condition ? `<p class="muted">Condition: ${esc(edge.condition)}</p>` : ""}</div>`;
  }
  if (selection.type === "edge" && editor.grouped) {
    const group = connectionGroups(graph).find(item => item.member_ids.includes(selection.id));
    if (group?.member_ids.length > 1) {
      element.querySelector("#groupMember").closest(".inspector-section").insertAdjacentHTML("beforeend", `<p class="muted" style="margin-top:10px;font-size:10px">The fields below edit one relationship. These actions affect the entire visible arrow.</p><div class="button-row"><button type="button" class="button" data-reconnect-group="${esc(selection.id)}" data-mutates>Reconnect all ${group.member_ids.length}</button><button type="button" class="button button-danger" data-remove-group="${esc(selection.id)}" data-mutates>Remove all ${group.member_ids.length}</button></div>`);
    }
  }
  renderControls();
}
function updateEdge(id, values) {
  editGraph(graph => {
    const edge = graph.edges.find(item => item.id === id);
    const rewired = ["source", "target", "kind"].some(key => key in values && values[key] !== edge[key]);
    if (rewired) validateConnection(graph, {...edge, ...values}, id);
    Object.assign(edge, values, {origin: "user"});
    if (rewired) Object.assign(edge, {status: "confirmed", evidence: [], condition: null});
  });
}
async function deleteSelection(selection = state.selection) {
  if (!canEdit() || !selection) return;
  const graph = state.session.graph;
  if (selection.type === "node") {
    const node = graph.nodes.find(item => item.id === selection.id);
    const count = graph.edges.filter(edge => edge.source === node.id || edge.target === node.id).length;
    if (!await confirmAction("Remove this node?", `${nodeName(node)} and its ${count} incident connection${count === 1 ? "" : "s"} will be removed from the draft. Source files are never deleted. You can undo before saving.`, "Remove node")) return;
    editGraph(next => { next.nodes = next.nodes.filter(item => item.id !== node.id); next.edges = next.edges.filter(edge => edge.source !== node.id && edge.target !== node.id); });
  } else {
    if (!await confirmAction("Remove this connection?", "Only this individual relationship will be removed. Other relationships grouped into the same visible arrow remain. You can undo before saving.", "Remove connection")) return;
    editGraph(next => { next.edges = next.edges.filter(edge => edge.id !== selection.id); });
  }
}
async function removeConnectionGroup(edgeId) {
  if (!canEdit()) return;
  const group = connectionGroups(state.session.graph).find(item => item.member_ids.includes(edgeId));
  if (!group || !await confirmAction("Remove this visible connection?", `All ${group.member_ids.length} saved relationships represented by this arrow will be removed, including their individual import/call records. Other arrows are kept. You can undo before saving.`, "Remove visible connection")) return;
  const ids = new Set(group.member_ids);
  editGraph(graph => { graph.edges = graph.edges.filter(edge => !ids.has(edge.id)); });
}
function openEditorDialog(action, sourceId = "") {
  if (action !== "suggest" && !canEdit()) return;
  state.editorAction = action;
  state.editorBase = {id: state.selectedId, revision: state.session.base.revision};
  const group = action === "group" ? connectionGroups(state.session.graph).find(item => item.member_ids.includes(sourceId)) : null;
  state.editorGroupIds = group?.member_ids ?? [];
  $("editorDialogError").hidden = true;
  const title = action === "node" ? "Add node" : action === "edge" ? "Add connection" : action === "group" ? "Reconnect visible connection" : "Suggest connections with AI";
  $("editorDialogTitle").textContent = title;
  $("editorDialogSubmit").textContent = action === "suggest" ? "Request suggestions" : title;
  if (action === "node") $("editorDialogFields").innerHTML = `<label class="field"><span>Node name</span><input name="label" required maxlength="1000" placeholder="For example, Review output"></label><label class="field"><span>Node type</span><select name="kind">${NODE_KINDS.map(kind => `<option value="${kind}">${esc(readable(kind))}</option>`).join("")}</select><small>A manual node does not impersonate a source script or inherit its summaries.</small></label>`;
  else if (action === "edge") $("editorDialogFields").innerHTML = `<label class="field"><span>From · source</span><select name="source">${nodeOptions(sourceId)}</select></label><label class="field"><span>To · destination</span><select name="target">${nodeOptions(state.session.graph.nodes.find(node => node.id !== sourceId)?.id)}</select></label><label class="field"><span>Connection type</span><select name="kind">${kindOptions("depends_on")}</select><small>Reads: resource → reader. Writes: writer → resource.</small></label><label class="field"><span>Review note · optional</span><textarea name="review_note"></textarea></label>`;
  else $("editorDialogFields").innerHTML = `<p class="muted">This sends saved source snapshots to the selected provider. Suggested connections remain unconfirmed until you review them. Existing corrections are retained.</p><label class="field"><span>Provider</span><select name="model"><option value="OpenAI" ${$("model").value === "OpenAI" ? "selected" : ""}>OpenAI / configured Azure</option><option value="Ollama" ${$("model").value === "Ollama" ? "selected" : ""}>Ollama · local model</option></select></label><div class="two-columns"><label class="field"><span>Concurrent requests</span><input name="max_concurrency" type="number" min="1" max="16" value="${Number($("maxConcurrency").value)}" required></label><label class="field"><span>Timeout (seconds)</span><input name="timeout_seconds" type="number" min="1" max="300" value="${Number($("timeoutSeconds").value)}" required></label></div><label class="checkbox-line"><input type="checkbox" name="consent" required><span>I agree to send the saved source text to this provider.</span></label>`;
  if (action === "group") $("editorDialogFields").innerHTML = `<p class="muted">Reconnect all ${group.member_ids.length} relationships in this arrow. Individual connection types stay unchanged. Evidence for the old endpoints remains in saved revision history.</p><label class="field"><span>From · source</span><select name="source">${nodeOptions(group.source)}</select></label><label class="field"><span>To · destination</span><select name="target">${nodeOptions(group.target)}</select></label><label class="field"><span>Review note · optional</span><textarea name="review_note"></textarea></label>`;
  $("editorDialog").showModal();
}

function diagnosticGroups() {
  if (state.selected.review?.diagnostics?.groups) return state.selected.review.diagnostics.groups;
  const groups = new Map();
  for (const issue of state.selected.review?.issues ?? []) {
    if (!groups.has(issue.code)) groups.set(issue.code, {code: issue.code, title: readable(issue.code), severity: issue.severity, occurrences: [], description: "Review the cited source to decide whether a connection should be added or corrected."});
    groups.get(issue.code).occurrences.push({...issue, source_path: issue.evidence?.[0]?.source_path, line_start: issue.evidence?.[0]?.line_start});
  }
  return [...groups.values()];
}
function renderFindings() {
  if (!state.session) return;
  const query = $("findingsSearch").value.toLowerCase();
  const severity = $("findingsSeverity").value;
  const graph = state.session.graph;
  const proposals = graph.edges.filter(edge => edge.status === "proposed");
  $("proposedConnections").innerHTML = proposals.length ? `<div class="proposed-review"><h4>${proposals.length} unconfirmed connection${proposals.length === 1 ? "" : "s"}</h4><p>Confirm, reconnect or remove these before generating. Changes still need to be saved.</p>${proposals.map(edge => `<div class="proposed-row"><span>${esc(nodeName(graph.nodes.find(node => node.id === edge.source)))} → ${esc(nodeName(graph.nodes.find(node => node.id === edge.target)))}<small>${esc(readable(edge.kind))}</small></span><div class="button-row"><button type="button" class="button button-small" data-select-edge="${esc(edge.id)}">Inspect</button><button type="button" class="button button-small" data-confirm-edge="${esc(edge.id)}" ${!canEdit() ? "disabled" : ""}>Confirm</button><button type="button" class="icon-button" data-remove-edge="${esc(edge.id)}" aria-label="Remove unconfirmed connection" ${!canEdit() ? "disabled" : ""}>${icon("trash")}</button></div></div>`).join("")}</div>` : "";
  const groups = diagnosticGroups().map(group => ({...group, occurrences: group.occurrences.filter(item => (severity === "all" || (item.severity ?? group.severity) === severity) && `${item.source_path ?? ""} ${item.message} ${group.title}`.toLowerCase().includes(query))})).filter(group => group.occurrences.length);
  $("findingsList").innerHTML = groups.length ? groups.map(group => {
    const all = state.expandedGroups.has(group.code);
    const items = all ? group.occurrences : group.occurrences.slice(0, 20);
    return `<details class="finding-group" data-group="${esc(group.code)}" ${group.severity === "error" ? "open" : ""}><summary><span>${esc(group.title)}</span><span class="badge ${esc(group.severity)}">${group.occurrences.length} · ${group.severity === "error" ? "error" : group.severity === "info" ? "information" : "review"}</span></summary><div class="finding-group-content">${group.description ? `<p>${esc(group.description)}</p>` : ""}${group.suggested_action ? `<p class="finding-action">${esc(group.suggested_action)}</p>` : ""}${items.map(item => `<div class="finding-occurrence"><strong>${esc(item.source_path ?? item.evidence?.[0]?.source_path ?? "Project")}${item.line_start ? `:${Number(item.line_start)}` : ""}</strong><p>${esc(item.message)}</p>${item.evidence?.[0]?.excerpt ? `<pre>${esc(item.evidence[0].excerpt)}</pre>` : ""}${item.node_ids?.some(id => graph.nodes.some(node => node.id === id)) ? `<button type="button" class="text-button" data-focus-node="${esc(item.node_ids.find(id => graph.nodes.some(node => node.id === id)))}">Inspect related node →</button>` : ""}</div>`).join("")}${!all && group.occurrences.length > 20 ? `<button class="text-button" type="button" data-expand-group="${esc(group.code)}">Show all ${group.occurrences.length} occurrences</button>` : ""}</div></details>`;
  }).join("") : `<div class="empty-state">${icon("check")}<h3>${query || severity !== "all" ? "No findings match this filter" : "No analysis findings"}</h3><p>Review the diagram against the real workflow before generating.</p></div>`;
}
function renderSources() {
  if (!state.session) return;
  const graph = state.session.graph;
  const query = $("sourcesSearch").value.toLowerCase();
  const diagnostics = state.selected.review?.diagnostics?.sources ?? [];
  const sourceRows = diagnostics.length ? diagnostics : graph.sources;
  $("sourceRows").innerHTML = sourceRows.filter(source => (source.path ?? source.source_path ?? "").toLowerCase().includes(query)).map(source => {
    const path = source.path ?? source.source_path;
    const node = graph.nodes.find(item => item.kind === "script" && item.source_path === path);
    const label = {parsed: "Analyzed", partial: "Needs review", failed: "Analysis failed", skipped: "Skipped"}[source.status] ?? source.status;
    return `<tr><td>${esc(path)}${source.description ? `<small>${esc(source.description)}</small>` : ""}</td><td>${esc(readable(source.script_type ?? graph.sources.find(item => item.path === path)?.script_type))}</td><td><span class="badge ${source.status === "failed" ? "error" : source.status === "partial" ? "warning" : source.status === "skipped" ? "info" : ""}">${esc(label)}</span></td><td>${node ? `<button type="button" class="text-button" data-focus-node="${esc(node.id)}">Inspect →</button>` : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="4">No source files match this filter.</td></tr>';
}

function modelOptions() {
  const concurrency = Number($("maxConcurrency").value);
  const timeout = Number($("timeoutSeconds").value);
  return {model: $("model").value === "Ollama" ? "Ollama" : "OpenAI", language: $("language").value || "English", max_concurrency: Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 16 ? concurrency : 1, timeout_seconds: Number.isFinite(timeout) && timeout > 0 && timeout <= 300 ? timeout : 90};
}
function saveOptions() {
  if (!state.selectedId) return;
  if (!preferences.options || typeof preferences.options !== "object" || Array.isArray(preferences.options)) preferences.options = {};
  preferences.options[state.selectedId] = {...modelOptions(), use_llm: $("useLlmSummaries").checked};
  savePreferences();
}
/*
Render readiness, optional enrichment and generation-specific downloads.
- Opening this screen does not send source code to an AI provider.
- Show explicit opt-ins for proposed connections or incomplete analysis.
- Reuse the saved artifact addresses returned by the backend.
*/
function renderGeneration() {
  if (!state.session) return;
  const {graph, dirty, conflict} = state.session;
  const review = state.selected.review ?? {};
  const proposed = graph.edges.filter(edge => edge.status === "proposed").length;
  const generation = state.selected.generation;
  const flowchart = artifactURL(state.selected.outputs?.flowchart);
  const useModel = $("useLlmSummaries").checked;
  $("providerFields").hidden = !useModel;
  $("providerFields").querySelectorAll("input,select").forEach(control => { control.disabled = !useModel; });
  $("localSummaryNote").hidden = useModel;
  $("proposedOption").hidden = !proposed;
  $("incompleteOption").hidden = !review.has_analysis_errors;
  $("generationReadiness").innerHTML = dirty || conflict ? `<div class="notice notice-warning"><div><strong>Save your review first</strong><p>${conflict ? "Resolve the revision conflict before generating." : "There are unsaved diagram changes. Generation only uses the saved revision."}</p><button type="button" class="text-button" data-go-review>Return to Review →</button></div></div>` : !graph.nodes.length ? '<div class="notice notice-warning">This draft has no nodes. Analyze supported files or add nodes before generating.</div>' : `<div class="readiness-line">${icon("check")}Revision ${state.selected.revision} · ${graph.nodes.length} nodes · ${graph.edges.length} saved relationships</div>${proposed ? `<div class="notice notice-warning"><div>${proposed} connection${proposed === 1 ? " is" : "s are"} still unconfirmed. Review them, or explicitly include marked suggestions.</div></div>` : ""}${review.has_analysis_errors ? '<div class="notice notice-warning"><div>Some sources could not be analyzed. Review the errors and acknowledge incomplete coverage to continue.</div></div>' : ""}`;
  const counts = generation?.summary_status_counts ?? {};
  const fallback = Number(counts.fallback ?? 0);
  const local = Number(counts.deterministic ?? 0);
  const success = Number(counts.complete ?? counts.completed ?? counts.generated ?? counts.success ?? 0);
  const mode = fallback ? `${fallback} AI fallback${fallback === 1 ? "" : "s"}` : generation?.settings?.use_llm ? "AI enhanced" : "Local descriptions";
  $("generationResult").innerHTML = `<p class="eyebrow">${generation && flowchart ? "READY TO SHARE" : "YOUR FINISHED FLOWCHART"}</p><div class="result-illustration"><div class="result-document">${icon("flow")}<span>HTML</span></div>${generation && flowchart ? `<span class="result-ready-badge">${icon("check")}</span>` : ""}</div><h2>${generation && flowchart ? "Your workflow, connected." : "Clarity, with the details built in."}</h2><p>${generation && flowchart ? `Generated from revision ${generation.revision}. Open the interactive chart or download its standalone HTML file.` : "The finished chart adds expandable script summaries, evidence and navigation to the connections you reviewed."}</p>${generation && flowchart ? `<div class="result-actions"><a class="button button-primary" id="openFlowchartLink" href="${esc(flowchart)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open flowchart</a><button type="button" class="button" data-download="flowchart_download">${icon("download")}Download HTML</button></div><div class="result-meta"><span>Generated<strong>${esc(dateText(generation.created_at))}</strong></span><span>Summaries<strong>${esc(mode)}</strong></span></div>${fallback ? `<div class="notice notice-warning"><div><strong>${fallback} script summaries used fallback text</strong><p>The model did not return a usable result for those scripts. Your reviewed connections were preserved. Download summary details for individual reasons.</p></div></div>` : local && generation.settings?.use_llm ? `<p class="quiet-note">${local} local descriptions${success ? `; ${success} model summaries` : ""}.</p>` : ""}<div class="result-minor-links"><button type="button" data-download="summaries">Summary details (JSON)</button><button type="button" data-download="flowchart_spec">Reviewed graph (JSON)</button><button type="button" data-download="review">Analysis review (JSON)</button></div>` : `<div class="result-meta"><span>Format<strong>Interactive HTML</strong></span><span>Connections<strong>Kept as reviewed</strong></span></div>`}`;
  renderControls();
}

async function downloadArtifact(name) {
  if (!state.online) throw new Error("Reconnect to the local server before downloading saved artifacts.");
  let raw = state.selected?.outputs?.[name];
  if (name === "flowchart_download" && !raw) {
    const existing = artifactURL(state.selected?.outputs?.flowchart);
    if (existing) { const url = new URL(existing); url.searchParams.set("download", "1"); raw = url.href; }
  }
  const url = artifactURL(raw);
  if (!url) throw new Error("This artifact is not available for the selected revision. Generate the flowchart from this revision first.");
  try {
    const response = await request(url, {raw: true, timeout: 30000});
    const filenames = {flowchart_download: "workflow-flowchart.html", draft_diagram: `workflow-r${state.selected.revision}.drawio`, summaries: "summary-details.json", flowchart_spec: "reviewed-graph.json", review: "analysis-review.json"};
    const filename = downloadFilename(response.headers.get("Content-Disposition"), filenames[name] ?? "workflow-artifact");
    saveBlob(await response.blob(), filename);
  } catch (error) {
    if (error.status === 404) throw new Error("This generated file is missing from its saved location. Your draft is still available; generate a fresh flowchart to recreate the download.");
    throw error;
  }
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.rel = "noopener";
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function downloadRecovery() {
  if (!state.session) return;
  saveBlob(new Blob([JSON.stringify(state.session.recovery(), null, 2)], {type: "application/json"}), "unsaved-diagram-edits.json");
}

async function submitJob(kind, payload, id = state.selectedId) {
  if (!state.online) throw new Error("Reconnect to the local server before starting a new operation. Your edits are kept in this browser.");
  if (kind !== "analyze" && busyDraft()) throw new Error("Wait for this draft's active operation to finish.");
  const requestId = crypto.randomUUID();
  const body = {kind, payload, request_id: requestId, ...(kind === "analyze" ? {} : {draft_id: id})};
  // - A failed browser write prevents submission, because replaying an unknown
  //   mutation without its original idempotency key would not be safe.
  pending.prepare("/api/jobs", body);
  if (kind === "edit" && state.session) { state.session.submittedRequestId = requestId; persistEdits(); }
  $("appMessage").hidden = true;
  renderControls(); renderTaskNotice();
  await recoverPending();
}
async function recoverPending() {
  if (!pending.value || !state.online) return;
  try {
    const job = await pending.send();
    if (!job) return;
    state.watchJobId = job.id;
    state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)];
    savePreferences(); renderActivity(); renderTaskNotice(); renderControls();
    await handleWatchedJob();
  } catch (error) {
    // - Unknown responses remain recoverable with their existing key.
    // - An HTTP validation/revision rejection is definite and may be corrected.
    showMessage(error.uncertain ? "The request may already be saved. Reconnecting will recover its result using the same request ID; no new operation will be started." : error.message, error.uncertain ? "warning" : "error");
    renderControls();
  }
}
async function retryJob(id) {
  const job = state.jobs.find(item => item.id === id);
  if (!job || !["failed", "interrupted"].includes(job.state)) return;
  if (!await confirmAction("Retry this operation?", "The same saved inputs and base revision will be used. If this operation uses a model, retrying may incur another provider request or charge. A stale revision still needs a fresh review.", "Retry operation")) return;
  const retryId = crypto.randomUUID();
  pending.prepare(`/api/jobs/${encodeURIComponent(id)}/retry`, {request_id: retryId});
  if (job.kind === "edit" && state.session?.submittedRequestId === job.request_id) {
    state.session.submittedRequestId = retryId; persistEdits();
  }
  renderControls();
  await recoverPending();
}
async function handleWatchedJob() {
  const job = state.jobs.find(item => item.id === state.watchJobId);
  if (!job || activeStates.has(job.state) || state.handled.has(job.id)) return;
  if (job.state === "succeeded") {
    const id = job.result?.draft_id ?? job.draft_id;
    if (id) {
      const result = await request(`/api/drafts/${encodeURIComponent(id)}`);
      const shouldSelect = !state.selectedId || state.selectedId === id || job.kind === "analyze";
      if (shouldSelect) takeDraft(result, {fit: job.kind === "analyze", step: job.kind === "generate" ? "generate" : "review", completedRequest: job.kind === "edit" ? job.request_id : null});
      else if (storage.read(`edits.${id}`)?.submitted_request_id === job.request_id) storage.remove(`edits.${id}`);
    }
    showMessage(`${jobNames[job.kind] ?? "Operation"} completed and saved.`);
    state.lastLibrary = 0;
  } else {
    showMessage(job.error?.message ?? (job.state === "interrupted" ? "This operation was interrupted when the server stopped. Review its status and choose Retry when ready." : "The operation did not finish. Your previously saved draft remains available."), "warning");
    if (job.error?.code === "revision_conflict" && job.draft_id === state.selectedId) {
      const result = await request(`/api/drafts/${encodeURIComponent(job.draft_id)}`);
      takeDraft(result, {step: "review"});
    }
  }
  state.handled.add(job.id);
  savePreferences(); renderTaskNotice(); renderControls();
}
function renderTaskNotice() {
  const element = $("taskNotice");
  const uncertain = pending.value;
  const job = currentJob();
  renderProgress();
  renderRunLog();
  if (uncertain) {
    element.hidden = false;
    element.className = "notice task-notice notice-warning";
    element.innerHTML = `<span class="spinner" aria-hidden="true"></span><div><strong>Recovering the request</strong><p>The exact request is kept in this browser. We will check for its saved job when the server is available.</p></div>`;
    return;
  }
  if (!job || job.state === "succeeded") { element.hidden = true; return; }
  const active = activeStates.has(job.state);
  element.hidden = false;
  element.className = `notice task-notice ${active ? "" : "notice-warning"}`;
  element.innerHTML = `${active ? '<span class="spinner" aria-hidden="true"></span>' : icon("info")}<div><strong>${esc(jobNames[job.kind] ?? "Operation")} · ${esc(readable(job.state))}</strong><p>${esc(active ? job.logs?.at(-1)?.message ?? "The operation is saved. You can close this browser while the server works." : job.error?.message ?? "Review the saved operation status before retrying.")}</p></div><button type="button" class="button button-small" data-open-activity>Details</button>${!active ? `<button type="button" class="button button-small" data-retry-job="${esc(job.id)}">Retry operation</button>` : ""}`;
}

/*
Render the compact log used by the restored analysis screen.

- Messages come from the backend's retained job log; the preview never invents
  parser progress or infer success from a browser connection.
- Only the newest entries are shown here. The Activity drawer keeps the complete
  retained history, retry controls and interrupted-job explanations.
- All backend text is escaped before insertion because a source filename or parser
  message may contain characters that have meaning in HTML.
*/
function renderRunLog() {
  const job = currentJob() ?? state.jobs[0];
  const status = $("runLogStatus");
  const preview = $("runLogPreview");
  if (!job) {
    status.className = "badge info";
    status.textContent = pending.value ? "Recovering" : "Ready";
    preview.innerHTML = `<p class="run-log-empty">${pending.value ? "Recovering the saved request when the local server reconnects." : "No operations yet. Start an analysis to see progress here."}</p>`;
    return;
  }
  const active = activeStates.has(job.state);
  status.className = `badge ${job.state === "failed" ? "error" : job.state === "interrupted" ? "warning" : active ? "" : "info"}`;
  status.textContent = readable(job.state);
  const entries = (job.logs ?? []).slice(-7);
  if (!entries.length) {
    preview.innerHTML = `<p class="run-log-row"><span class="run-log-dot" aria-hidden="true"></span><time>${esc(dateText(job.created_at))}</time><span>${esc(jobNames[job.kind] ?? readable(job.kind))} · ${esc(readable(job.state))}</span></p>`;
    return;
  }
  preview.innerHTML = entries.map(log => `<p class="run-log-row"><span class="run-log-dot" aria-hidden="true"></span><time>${esc(dateText(log.created_at))}</time><span>${esc(log.message)}</span></p>`).join("");
}

function renderActivity() {
  const openIds = new Set([...$("activityList").querySelectorAll("details[open]")].map(details => details.dataset.logsFor));
  const active = state.jobs.filter(job => activeStates.has(job.state)).length;
  $("activityCount").hidden = !active; $("activityCount").textContent = active;
  $("activityList").innerHTML = state.jobs.length ? state.jobs.map(job => `<article class="job-card"><div class="job-card-header"><h3>${esc(jobNames[job.kind] ?? job.kind)}</h3><span class="badge ${job.state === "failed" ? "error" : job.state === "interrupted" ? "warning" : ""}">${esc(readable(job.state))}</span></div><time>${esc(dateText(job.created_at))}</time>${job.error?.message ? `<p>${esc(job.error.message)}</p>` : ""}<div class="button-row">${job.draft_id ? `<button type="button" class="button" data-draft="${esc(job.draft_id)}">Open workflow</button>` : ""}${["failed", "interrupted"].includes(job.state) ? `<button type="button" class="button" data-retry-job="${esc(job.id)}">Retry operation</button>` : ""}${job.result?.outputs?.flowchart && artifactURL(job.result.outputs.flowchart) ? `<a class="button" href="${esc(artifactURL(job.result.outputs.flowchart))}" target="_blank" rel="noopener noreferrer">Open this result</a>` : ""}</div><details data-logs-for="${esc(job.id)}" ${openIds.has(job.id) ? "open" : ""}><summary>Run details</summary><div class="job-logs">${(job.logs ?? []).map(log => `<p><time>${esc(dateText(log.created_at))}</time>${esc(log.message)}</p>`).join("") || "No run messages yet."}</div>${job.logs_truncated ? `<button type="button" class="text-button" data-load-log="${esc(job.id)}">Load full retained log</button>` : ""}</details></article>`).join("") : '<div class="empty-state"><h3>No operations yet</h3><p>Analysis, edits, imports and generation will appear here.</p></div>';
  renderRunLog(); renderProgress(); renderControls();
}

/*
Reconnect the interface to durable backend state.
- Avoid overlapping polls and recover uncertain submissions with their original ID.
- Distinguish a connection failure from a failed analysis or generation job.
- Keep cached drafts and local edits usable while the backend is unavailable.
*/
async function synchronize(force = false) {
  if (state.polling || state.stopping) return;
  state.polling = true;
  try {
    const health = await request("/api/health", {timeout: 4500});
    if (health.app_id !== "da-workflow") throw new Error("This address is serving a different application. Reopen DA Document Generator with its launcher.");
    if (health.status !== "ok") throw new Error("The local server is stopping. Reopen the launcher to continue.");
    const restarted = state.health && state.health.instance_id !== health.instance_id;
    state.health = health; state.online = true;
    $("connectionStatus").dataset.state = "online";
    $("connectionStatus").innerHTML = "<i></i>Local server connected";
    $("connectionBanner").hidden = true;
    $("serverDetails").textContent = `${health.managed ? "Launcher-managed" : "Manually started"} server · ${health.worker_state ?? "connected"}. ${health.project_root ?? ""}`;
    if (restarted) { state.lastLibrary = 0; showMessage("The server is back. Saved jobs and revisions have been reloaded; interrupted work will not restart automatically."); }
    await recoverPending();
    const fetchLibrary = force || Date.now() - state.lastLibrary > 15000;
    // - Poll compact job metadata; full graphs are fetched only for a selected
    //   or changed revision, keeping large project polling inexpensive.
    const results = await Promise.allSettled([request("/api/jobs?summary=true"), ...(fetchLibrary ? [request("/api/drafts")] : [])]);
    if (results[0].status === "fulfilled") {
      state.jobs = results[0].value.map(job => {
        const existing = state.jobs.find(item => item.id === job.id);
        return existing?.result_complete && existing.updated_at === job.updated_at ? existing : job;
      });
      await handleWatchedJob();
    } else throw results[0].reason;
    if (fetchLibrary) {
      if (results[1].status === "rejected") throw results[1].reason;
      state.drafts = results[1].value;
      state.lastLibrary = Date.now();
      safeWrite("library", state.drafts);
      renderLibrary();
      const row = state.drafts.find(item => draftId(item) === state.selectedId);
      if (row && (!state.selected || row.revision !== state.session?.base.revision || (row.generation_id ?? null) !== (state.selected.generation?.generation_id ?? null))) {
        await loadDraft(state.selectedId, {step: state.step === "analyze" ? "review" : state.step, quiet: true});
      }
    }
    renderActivity(); renderTaskNotice(); renderControls();
  } catch (error) {
    state.online = false;
    $("connectionStatus").dataset.state = "offline";
    $("connectionStatus").innerHTML = "<i></i>Connection paused";
    $("connectionBanner").hidden = false;
    $("connectionMessage").textContent = error.status && error.status < 500 ? `The server rejected a status request: ${error.message}` : `${error instanceof APIError ? "Reopen the launcher if the local server was stopped." : error.message} Saved drafts and browser edits are retained. A lost connection does not mean your job failed.`;
    $("serverDetails").textContent = "Disconnected. Reopen the launcher to reconnect to saved workflows.";
    renderTaskNotice(); renderControls();
  } finally {
    state.polling = false;
  }
}

/*
Bind user actions after the static page and editor are initialized.

- Event delegation also covers dynamically rendered evidence and saved jobs.
- Forms preserve keyboard submission and browser validation on Windows/macOS.
- No page-unload action shuts down the backend or cancels a durable job.
*/
on("analysisForm", "submit", async event => {
  event.preventDefault(); savePreferences();
  const optional = id => $(id).value.trim() || null;
  const options = modelOptions();
  await submitJob("analyze", {script_folder: $("scriptFolder").value.trim(), da_document_folder: $("documentFolder").value.trim(), title: $("workflowName").value.trim(), working_directory: optional("workingDirectory"), sql_dialect: optional("sqlDialect"), database_namespace: optional("databaseNamespace"), model: options.model, language: options.language, max_concurrency: options.max_concurrency}, null);
});
on("generationForm", "submit", async event => {
  event.preventDefault();
  if (!state.session || state.session.dirty || state.session.conflict) throw new Error("Save or discard the diagram changes before generating.");
  saveOptions();
  await submitJob("generate", {expected_revision: state.selected.revision, ...modelOptions(), use_llm: $("useLlmSummaries").checked, allow_proposed: $("allowProposedEdges").checked, acknowledge_incomplete: $("acknowledgeIncomplete").checked});
});
on("saveChangesButton", "click", async () => {
  const changes = state.session.changes;
  if (!changes.length) return;
  if (changes.length > 1000) throw new Error("This edit exceeds 1,000 operations. Undo some changes and save smaller batches.");
  await submitJob("edit", {expected_revision: state.session.base.revision, operations: changes});
});
async function discardEdits() {
  if (busyDraft() || !state.session) return;
  if (!await confirmAction("Discard local diagram changes?", "The saved server revision will be kept. All unsaved node and connection changes in this browser will be discarded. Download unsaved edits first if you need a reference copy.", "Discard changes")) return;
  state.session.discard(); persistEdits(); state.selection = null; editor.selection = null;
  editor.setGraph(state.session.graph); renderReview(); renderInspector();
}
on("discardButton", "click", discardEdits);
on("discardConflictButton", "click", discardEdits);
on("downloadRecoveryButton", "click", downloadRecovery);
function undo(redo = false) {
  if (!canEdit()) return;
  if (redo ? state.session.redo() : state.session.undo()) {
    persistEdits(); editor.setGraph(state.session.graph); renderReview(); renderInspector();
  }
}
on("undoButton", "click", () => undo());
on("redoButton", "click", () => undo(true));
on("newAnalysisButton", "click", newAnalysis);
on("brandHome", "click", newAnalysis);
on("librarySearch", "input", renderLibrary);
on("refreshLibrary", "click", () => synchronize(true));
on("reconnectButton", "click", () => { state.stopping = false; return synchronize(true); });
on("goGenerateButton", "click", () => setStep("generate"));
on("canvasSearch", "input", () => editor.setFilter({query: $("canvasSearch").value}));
on("canvasScope", "change", () => {
  if ($("canvasScope").value === "neighbors" && !state.selection) showMessage("Select a node to focus on its direct neighbors. No saved connections will be removed.");
  editor.setFilter({scope: $("canvasScope").value});
});
on("relationshipView", "change", () => { editor.grouped = relationshipView.value === "direct"; editor.render(); renderInspector(); });
on("fitButton", "click", () => editor.fit());
on("zoomInButton", "click", () => editor.zoom(1.25));
on("zoomOutButton", "click", () => editor.zoom(0.8));
on("addNodeButton", "click", () => openEditorDialog("node"));
on("addEdgeButton", "click", () => openEditorDialog("edge", state.selection?.type === "node" ? state.selection.id : ""));
on("suggestButton", "click", () => openEditorDialog("suggest"));
on("findingsSearch", "input", renderFindings);
on("findingsSeverity", "change", renderFindings);
on("sourcesSearch", "input", renderSources);
on("downloadDrawioButton", "click", () => downloadArtifact("draft_diagram"));
on("importDrawioButton", "click", () => $("importFile").click());
on("importFile", "change", async () => {
  const file = $("importFile").files[0]; $("importFile").value = "";
  if (!file) return;
  if (state.session.dirty || state.session.conflict) throw new Error("Save or discard browser edits before importing another diagram.");
  if (file.size > 10000000) throw new Error("The diagram exceeds the 10 MB import limit.");
  const xml = await file.text();
  if (!await confirmAction("Import the corrected diagram?", "The imported topology and positions will become a new saved revision. Script source links stay attached to their original identities. Use a single page and layer; custom styles and bend points are not imported.", "Import corrections")) return;
  await submitJob("import", {expected_revision: state.selected.revision, xml});
});
on("editorDialogForm", "submit", async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    if (state.editorBase.id !== state.selectedId || state.editorBase.revision !== state.session?.base.revision || state.session.conflict) throw new Error("The saved revision changed while this dialog was open. Close it and review the current draft first.");
    if (state.editorAction === "node") {
      const id = newId("node");
      if (!values.label.trim()) throw new Error("Enter a node name, not just spaces.");
      if (!editGraph(graph => { graph.nodes.push({id, label: values.label.trim(), kind: values.kind, position: {x: Math.round(editor.view.x + editor.view.width / 2 - 150), y: Math.round(editor.view.y + editor.view.height / 2 - 41)}, details: {}}); })) return;
      $("editorDialog").close(); selectItem({type: "node", id});
    } else if (state.editorAction === "edge") {
      const edge = {id: newId("edge"), source: values.source, target: values.target, kind: values.kind, review_note: values.review_note.trim() || null, label: null, origin: "user", status: "confirmed", evidence: []};
      validateConnection(state.session.graph, edge);
      if (!editGraph(graph => graph.edges.push(edge))) return;
      $("editorDialog").close(); selectItem({type: "edge", id: edge.id});
    } else if (state.editorAction === "group") {
      const ids = new Set(state.editorGroupIds);
      const members = state.session.graph.edges.filter(edge => ids.has(edge.id));
      if (members.length !== ids.size) throw new Error("The selected connection changed. Close this dialog and inspect it again.");
      for (const member of members) {
        if ((values.source !== member.source || values.target !== member.target) && state.session.graph.edges.some(edge => !ids.has(edge.id) && edge.source === values.source && edge.target === values.target && edge.kind === member.kind)) throw new Error("A relationship at these endpoints already exists. Inspect that arrow before reconnecting this group.");
      }
      if (!editGraph(graph => { for (const edge of graph.edges) if (ids.has(edge.id)) {
        const rewired = edge.source !== values.source || edge.target !== values.target;
        Object.assign(edge, {source: values.source, target: values.target, origin: "user"});
        if (values.review_note.trim()) edge.review_note = values.review_note.trim();
        if (rewired) Object.assign(edge, {evidence: [], condition: null, status: "confirmed"});
      } })) return;
      $("editorDialog").close();
    } else {
      if (!values.consent) throw new Error("Confirm provider access before requesting suggestions.");
      $("editorDialog").close();
      await submitJob("suggest", {expected_revision: state.selected.revision, model: values.model, max_concurrency: Number(values.max_concurrency), timeout_seconds: Number(values.timeout_seconds)});
    }
  } catch (error) { $("editorDialogError").textContent = error.message; $("editorDialogError").hidden = false; if (!$("editorDialog").open) showError(error); }
});
for (const id of formFields) on(id, "input", savePreferences);
for (const id of [...optionFields, "useLlmSummaries"]) on(id, "change", () => { saveOptions(); renderGeneration(); });
for (const id of ["allowProposedEdges", "acknowledgeIncomplete"]) on(id, "change", renderControls);
on("settingsButton", "click", () => { renderControls(); $("settingsDialog").showModal(); });
on("activityButton", "click", () => { renderActivity(); $("activityDialog").showModal(); });
on("toggleLibrary", "click", () => setLibraryOpen(!$("librarySidebar").classList.contains("is-open")));
on("closeLibrary", "click", () => setLibraryOpen(false, {returnFocus: true}));
on("themeOptions", "change", event => {
  if (event.target.name === "accentTheme") applyAppearance(event.target.value, {persist: true});
});
on("stopServerButton", "click", async () => {
  if (!await confirmAction("Stop the local server?", "Saved drafts will remain on this computer. The app will disconnect until you run its launcher again. Active jobs must finish before the server can be stopped.", "Stop server")) return;
  persistEdits();
  await request("/api/shutdown", {method: "POST", body: {instance_id: state.health.instance_id}});
  state.stopping = true; state.online = false;
  $("settingsDialog").close();
  $("connectionBanner").hidden = false;
  $("connectionMessage").textContent = "The server is stopping. Reopen the launcher, then choose Check connection. Saved drafts remain available.";
  $("connectionStatus").dataset.state = "offline";
  $("connectionStatus").innerHTML = "<i></i>Server stopped";
  renderControls();
});

document.addEventListener("click", async event => {
  const target = event.target.closest("button,a");
  if (!target || target.disabled) return;
  try {
    if (target.dataset.step) setStep(target.dataset.step);
    else if (target.dataset.reviewTab) setReviewTab(target.dataset.reviewTab);
    else if (target.hasAttribute("data-dismiss-message")) $("appMessage").hidden = true;
    else if (target.dataset.closeDialog) $(target.dataset.closeDialog).close();
    else if (target.dataset.draft) { $("activityDialog").close(); await loadDraft(target.dataset.draft, {step: "review"}); }
    else if (target.dataset.selectEdge) selectItem({type: "edge", id: target.dataset.selectEdge});
    else if (target.dataset.focusNode) { setStep("review"); selectItem({type: "node", id: target.dataset.focusNode}, {focus: true}); }
    else if (target.dataset.connectFrom) openEditorDialog("edge", target.dataset.connectFrom);
    else if (target.dataset.removeNode) await deleteSelection({type: "node", id: target.dataset.removeNode});
    else if (target.dataset.removeEdge) await deleteSelection({type: "edge", id: target.dataset.removeEdge});
    else if (target.dataset.removeGroup) await removeConnectionGroup(target.dataset.removeGroup);
    else if (target.dataset.reconnectGroup) openEditorDialog("group", target.dataset.reconnectGroup);
    else if (target.dataset.confirmEdge) updateEdge(target.dataset.confirmEdge, {status: "confirmed"});
    else if (target.dataset.retryJob) await retryJob(target.dataset.retryJob);
    else if (target.hasAttribute("data-open-activity")) { renderActivity(); $("activityDialog").showModal(); }
    else if (target.hasAttribute("data-go-review")) setStep("review");
    else if (target.dataset.download) await downloadArtifact(target.dataset.download);
    else if (target.hasAttribute("data-download-recovery")) downloadRecovery();
    else if (target.dataset.loadLog) { const job = await request(`/api/jobs/${encodeURIComponent(target.dataset.loadLog)}`); state.jobs = state.jobs.map(item => item.id === job.id ? job : item); renderActivity(); }
    else if (target.dataset.expandGroup) { const group = target.dataset.expandGroup; state.expandedGroups.add(group); renderFindings(); $("findingsList").querySelector(`[data-group="${CSS.escape(group)}"]`).open = true; }
    else if (target.dataset.folderTarget) {
      const input = $(target.dataset.folderTarget); target.disabled = true;
      try { const result = await request("/api/browse-folder", {method: "POST", body: {target: input.id, current_path: input.value || null}, timeout: 180000}); if (result.path) { input.value = result.path; savePreferences(); } }
      finally { target.disabled = false; }
    }
  } catch (error) { showError(error); }
});
$("inspector").addEventListener("change", event => {
  if (event.target.id === "jumpNode" && event.target.value) selectItem({type: "node", id: event.target.value}, {focus: true});
  if (event.target.id === "groupMember") selectItem({type: "edge", id: event.target.value});
});
$("inspector").addEventListener("submit", event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  if (event.target.id === "nodeInspectorForm") editGraph(graph => { const node = graph.nodes.find(item => item.id === state.selection.id); node.label = values.label.trim(); node.position = {x: Number(values.x), y: Number(values.y)}; });
  else if (event.target.id === "edgeInspectorForm") updateEdge(state.selection.id, {...values, label: values.label || null, review_note: values.review_note || null});
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("librarySidebar").classList.contains("is-open") && !document.querySelector("dialog[open]")) {
    setLibraryOpen(false, {returnFocus: true});
  }
  const editableInput = event.target.closest("input,textarea,select,[contenteditable=true]");
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !editableInput && !document.querySelector("dialog[open]")) {
    event.preventDefault(); undo(event.shiftKey);
  }
  const tab = event.target.closest("[data-review-tab]");
  if (tab && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault(); const tabs = ["diagram", "findings", "sources"]; const index = tabs.indexOf(tab.dataset.reviewTab); const next = tabs[(index + (event.key === "ArrowRight" ? 1 : 2)) % 3]; setReviewTab(next); $("tab-" + next).focus();
  }
});
window.addEventListener("beforeunload", event => {
  // - Normal closure is safe: durable jobs continue and local edits are saved.
  // - Warn only when browser storage actually failed to preserve local changes.
  if (state.storageUnsafe && state.session?.dirty) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("storage", event => {
  // - A palette change in another tab affects appearance only, never its graph.
  if (event.key === null || event.key === storage.prefix + "appearance.theme") applyAppearance(storage.read("appearance.theme"));
  if (event.key?.includes(".pending-request.") || event.key?.endsWith("library")) synchronize(true);
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) synchronize(true); });

// - Populate local state before connecting, so cached drafts and unsaved edits
//   remain inspectable even when the backend must be restarted first.
applyAppearance(storage.read("appearance.theme"));
for (const id of formFields) if (preferences.form?.[id] !== undefined) $(id).value = preferences.form[id];
renderLibrary();
const cached = storage.read("last-draft");
if (cached?.draft_id === state.selectedId && cached.graph) {
  try { takeDraft(cached, {fit: true, step: state.step === "analyze" ? "review" : state.step}); }
  catch { state.selected = null; state.session = null; setStep("analyze"); }
} else setStep("analyze");
renderControls(); renderTaskNotice();
if (location.protocol === "file:") showMessage("Open this app through its launcher. Opening index.html as a local file cannot connect to the backend.", "warning");
async function poll() { await synchronize(); setTimeout(poll, document.hidden ? 6000 : 3000); }
poll();
