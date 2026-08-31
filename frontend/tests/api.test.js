/*
Regression checks for request delivery and browser recovery.

- Exercise the production API module without a browser or live backend.
- Model shared Storage so two tabs can interleave reads and writes deliberately.
- Require uncertain requests to preserve their exact identity and payload.
- Verify that completing an older request cannot delete another tab's pending work.
*/

import test from "node:test";
import assert from "node:assert/strict";
import {APIError, PendingRequest, RecoveryStorage, artifactURL, downloadFilename, request} from "../api.js";

class MemoryStorage {
  constructor() { this.values = new Map(); this.beforeSet = null; }
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) {
    this.beforeSet?.(key, value);
    this.values.set(key, String(value));
  }
  removeItem(key) { this.values.delete(key); }
}

const firstId = "11111111-1111-4111-8111-111111111111";
const secondId = "22222222-2222-4222-8222-222222222222";
const bodyFor = request_id => ({
  request_id, kind: "analyze", payload: {script_folder: "C:\\Project with spaces", da_document_folder: "C:\\Reports", title: "Reviewed project"},
});
const jobFor = body => ({id: `job_${body.request_id.replaceAll("-", "")}`, request_id: body.request_id,
  kind: body.kind ?? "analyze", state: "queued", draft_id: body.draft_id ?? null, result: null, error: null, logs: []});
const storageFor = memory => new RecoveryStorage(memory, "contract.");
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

test("request sends exact JSON and keeps the same-origin transport settings", async () => {
  const body = bodyFor(firstId);
  let observed;
  const response = await request("/api/jobs", {method: "POST", body, fetcher: async (path, options) => {
    observed = {path, options};
    return {ok: true, status: 202, json: async () => jobFor(body)};
  }});
  assert.equal(response.request_id, firstId);
  assert.equal(observed.path, "/api/jobs");
  assert.deepEqual(JSON.parse(observed.options.body), body);
  assert.equal(observed.options.credentials, "same-origin");
  assert.equal(observed.options.cache, "no-store");
  assert.equal(observed.options.headers["Content-Type"], "application/json");
});

test("definite HTTP rejection is distinguishable from unknown network outcome", async () => {
  await assert.rejects(request("/api/jobs", {fetcher: async () => ({ok: false, status: 409,
    json: async () => ({detail: "This draft has changed."})})}), error => {
    assert.equal(error.status, 409);
    assert.equal(error.uncertain, false);
    assert.match(error.message, /draft has changed/);
    return true;
  });
  await assert.rejects(request("/api/jobs", {fetcher: async () => { throw new TypeError("Connection ended"); }}), error => {
    assert.equal(error.uncertain, true);
    assert.equal(error.status, 0);
    return true;
  });
  await assert.rejects(request("/api/jobs", {fetcher: async () => ({ok: true, status: 202,
    json: async () => { throw new SyntaxError("Partial response"); }})}), error => error.uncertain === true);
});

test("request timeout aborts its transport and reports an uncertain outcome", async () => {
  let aborted = false;
  await assert.rejects(request("/api/jobs", {timeout: 10, fetcher: (path, {signal}) => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => { aborted = true; reject(new DOMException("Stopped", "AbortError")); }, {once: true});
  })}), error => error instanceof APIError && error.uncertain);
  assert.equal(aborted, true);
});

test("a reload replays the exact persisted request after a lost response", async () => {
  const memory = new MemoryStorage();
  const first = new PendingRequest(storageFor(memory), async () => { throw new APIError("Connection ended"); });
  const original = bodyFor(firstId);
  first.prepare("/api/jobs", original);
  original.payload.title = "Changed only in caller memory";
  await assert.rejects(first.send(), error => error.uncertain);
  const saved = first.value;
  assert.equal(saved.body.payload.title, "Reviewed project");
  const calls = [];
  const reloaded = new PendingRequest(storageFor(memory), async (path, options) => {
    calls.push({path, body: options.body});
    return jobFor(options.body);
  });
  const job = await reloaded.send();
  assert.equal(job.request_id, firstId);
  assert.deepEqual(calls, [{path: "/api/jobs", body: saved.body}]);
  assert.equal(reloaded.value, null);
});

test("definite rejection clears only its request, while server errors preserve recovery", async () => {
  for (const status of [409, 422, 500, 503]) {
    const pending = new PendingRequest(storageFor(new MemoryStorage()), async () => { throw new APIError("Rejected", status); });
    pending.prepare("/api/jobs", bodyFor(firstId));
    await assert.rejects(pending.send(), APIError);
    assert.equal(pending.value !== null, status >= 500);
  }
});

test("overlapping polls in one tab submit the pending request only once", async () => {
  const response = deferred();
  let calls = 0;
  const body = bodyFor(firstId);
  const pending = new PendingRequest(storageFor(new MemoryStorage()), async () => {
    calls += 1;
    return response.promise;
  });
  pending.prepare("/api/jobs", body);
  const sending = pending.send();
  assert.equal(await pending.send(), null);
  assert.equal(calls, 1);
  response.resolve(jobFor(body));
  assert.equal((await sending).request_id, firstId);
  assert.equal(pending.value, null);
});

test("simultaneous tab preparation retains both requests instead of overwriting one", async () => {
  const memory = new MemoryStorage();
  const observed = [];
  const transport = async (path, {body}) => { observed.push(body.request_id); return jobFor(body); };
  const firstTab = new PendingRequest(storageFor(memory), transport);
  const secondTab = new PendingRequest(storageFor(memory), transport);
  let interleaved = false;
  memory.beforeSet = () => {
    if (interleaved) return;
    interleaved = true;
    // - Tab B enters after tab A has checked for pending work but before A's
    //   write becomes visible, a possible interleaving between browser processes.
    secondTab.prepare("/api/jobs", bodyFor(secondId));
  };
  firstTab.prepare("/api/jobs", bodyFor(firstId));
  memory.beforeSet = null;
  for (let remaining = 0; firstTab.value && remaining < 3; remaining += 1) await firstTab.send();
  assert.deepEqual(observed.sort(), [firstId, secondId]);
  assert.equal(firstTab.value, null);
});

test("a late successful response cannot delete the next request prepared by another tab", async () => {
  const memory = new MemoryStorage();
  const delayed = deferred();
  const firstTab = new PendingRequest(storageFor(memory), () => delayed.promise);
  const secondTab = new PendingRequest(storageFor(memory), async (path, {body}) => jobFor(body));
  firstTab.prepare("/api/jobs", bodyFor(firstId));
  const firstSending = firstTab.send();
  // - Another tab recovers the same UUID and receives its accepted result first.
  // - It can then save a distinct request before the older response arrives.
  assert.equal((await secondTab.send()).request_id, firstId);
  secondTab.prepare("/api/jobs", bodyFor(secondId));
  delayed.resolve(jobFor(bodyFor(firstId)));
  await firstSending;
  assert.equal(secondTab.value.body.request_id, secondId);
  assert.equal((await secondTab.send()).request_id, secondId);
  assert.equal(secondTab.value, null);
});

test("a late definite rejection cannot delete another tab's pending request", async () => {
  const memory = new MemoryStorage();
  const delayed = deferred();
  const firstTab = new PendingRequest(storageFor(memory), () => delayed.promise);
  const secondTab = new PendingRequest(storageFor(memory), async (path, {body}) => jobFor(body));
  firstTab.prepare("/api/jobs", bodyFor(firstId));
  const firstSending = firstTab.send();
  await secondTab.send();
  secondTab.prepare("/api/jobs", bodyFor(secondId));
  delayed.reject(new APIError("Old request was rejected", 409));
  await assert.rejects(firstSending, APIError);
  assert.equal(secondTab.value.body.request_id, secondId);
});

test("blocked localStorage access preserves readable fallback and refuses unsaved delivery", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {configurable: true, get() { throw new DOMException("Denied", "SecurityError"); }});
  try {
    const storage = new RecoveryStorage();
    assert.deepEqual(storage.read("library", []), []);
    const pending = new PendingRequest(storage);
    assert.equal(pending.value, null);
    assert.throws(() => pending.prepare("/api/jobs", bodyFor(firstId)), /storage is full or disabled/);
  } finally {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
    else delete globalThis.localStorage;
  }
});

test("malformed storage and unrelated settings do not become workflow requests", async () => {
  const memory = new MemoryStorage();
  memory.setItem("contract.pending-request.broken", "{unfinished JSON");
  memory.setItem("other-app.pending-request.some-id", JSON.stringify({body: bodyFor(secondId)}));
  memory.setItem("contract.preferences", JSON.stringify({title: "Settings"}));
  const pending = new PendingRequest(storageFor(memory), async (path, {body}) => jobFor(body));
  assert.equal(pending.value, null);
  pending.prepare("/api/jobs", bodyFor(firstId));
  assert.equal((await pending.send()).request_id, firstId);
  assert.equal(pending.value, null);
  assert.notEqual(memory.getItem("other-app.pending-request.some-id"), null);
});

test("artifact links stay within this server and preserve generation-specific addresses", () => {
  const origin = "http://127.0.0.1:8000";
  const path = "/api/drafts/draft_one/generations/generation_one/workflow_flowchart.html?download=1";
  assert.equal(artifactURL(path, origin), origin + path);
  for (const value of ["https://other.example/" + path, "javascript:alert(1)", "file:///C:/Reports/result.html", "/api/output/result.html", "/frontend/index.html"]) {
    assert.equal(artifactURL(value, origin), null);
  }
});

test("download filenames use UTF-8 attachment names and remove path/control characters", () => {
  assert.equal(downloadFilename("attachment; filename=\"Revenue Operations.html\"", "flowchart.html"), "Revenue Operations.html");
  assert.equal(downloadFilename("attachment; filename=\"flowchart.html\"; filename*=UTF-8''%E6%94%B6%E5%85%A5.html", "flowchart.html"), "\u6536\u5165.html");
  assert.equal(downloadFilename("attachment; filename=\"../../bad\r\nname.html\"", "flowchart.html"), "badname.html");
  assert.equal(downloadFilename("attachment; filename*=UTF-8''%bad; filename=\"safe.html\"", "flowchart.html"), "safe.html");
  assert.equal(downloadFilename(null, "flowchart.html"), "flowchart.html");
});
