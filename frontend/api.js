/*
The browser's connection and recovery boundary.

- Use same-origin API routes; filesystem paths are never browser download URLs.
- Separate a definite HTTP rejection from an unknown result after a disconnect.
- Persist the exact mutation body and idempotency key before sending a job.
- Replay only that same request after a lost response. A retry of interrupted
  work is a separate, explicit user action with a new request ID.
- Keep the transport independent of the UI so failure cases can be tested.
*/

export class APIError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
    this.uncertain = status === 0 || status >= 500;
  }
}

function errorMessage(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(item => `${(item.loc ?? []).filter(part => part !== "body").join(" · ")}: ${item.msg ?? "Invalid value"}`).join("; ");
  return value?.message ?? "The server could not complete this request.";
}

/*
Send one bounded same-origin request and classify its outcome for safe recovery.

- A clear 4xx response is a definite rejection that the user can correct.
- A timeout, network failure, malformed response, or server error is uncertain;
  the caller must check the durable job record before attempting new work.
- Raw mode supports verified artifact downloads without changing transport rules.
*/
export async function request(path, {method = "GET", body, timeout = 15000, raw = false, fetcher = fetch} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetcher(path, {
      method, signal: controller.signal, cache: "no-store", credentials: "same-origin",
      headers: body === undefined ? {Accept: "application/json"} : {"Content-Type": "application/json", Accept: "application/json"},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new APIError(errorMessage(payload.detail ?? payload.error ?? `Request failed (${response.status}).`), response.status, payload);
    }
    return raw ? response : await response.json();
  } catch (error) {
    if (error instanceof APIError) throw error;
    throw new APIError(error.name === "AbortError" ? "The server has not responded yet. Checking for a saved result…" : "The local server is unavailable. Your saved work has not been deleted.");
  } finally {
    clearTimeout(timer);
  }
}

/*
Wrap browser storage so recovery behavior remains explicit and testable.

- Prefix every key to avoid colliding with unrelated data on the same origin.
- Treat unreadable or malformed entries as absent without deleting other records.
- Fail writes visibly because submitting a mutation without a saved request ID
  would make reconnect recovery unsafe.
*/
export class RecoveryStorage {
  constructor(storage, prefix = "da-workflow.v2.") {
    // - Some browsers throw while obtaining localStorage itself; keep the
    //   saved library readable even when recovery storage is unavailable.
    try { this.storage = storage ?? globalThis.localStorage; }
    catch { this.storage = null; }
    this.prefix = prefix;
  }
  read(key, fallback = null) {
    try { return JSON.parse(this.storage.getItem(this.prefix + key)) ?? fallback; }
    catch { return fallback; }
  }
  write(key, value) {
    try { this.storage.setItem(this.prefix + key, JSON.stringify(value)); }
    catch { throw new Error("Browser recovery storage is full or disabled. Export your unsaved edits before closing this page, then free site storage or use a normal browser window."); }
  }
  remove(key) {
    try { this.storage.removeItem(this.prefix + key); }
    catch { /* - A blocked storage removal must not hide a successful server save. */ }
  }
  entries(keyPrefix) {
    // - Take a snapshot of immutable per-request keys; a different tab can add
    //   another request without overwriting an in-flight recovery record.
    try {
      const entries = [];
      for (let index = 0; index < this.storage.length; index += 1) {
        const key = this.storage.key(index);
        if (key?.startsWith(this.prefix + keyPrefix)) {
          const relative = key.slice(this.prefix.length);
          const value = this.read(relative);
          if (value) entries.push({key: relative, value});
        }
      }
      return entries;
    } catch { return []; }
  }
}

/*
Own exactly-once delivery for browser mutations across reloads and disconnections.

- Persist the complete path and body before the first network request begins.
- Keep separate per-request keys so two tabs cannot overwrite each other's work.
- Replay the same request ID after an uncertain outcome and remove it only after a
  valid accepted response or a definite client-side rejection.
*/
export class PendingRequest {
  constructor(storage, transport = request) {
    this.storage = storage;
    this.transport = transport;
    this.sending = false;
  }
  get records() {
    return this.storage.entries("pending-request.")
      .filter(entry => {
        const record = entry.value;
        return record && typeof record.body === "object" && typeof record.body?.request_id === "string" &&
          entry.key === `pending-request.${record.body.request_id}` &&
          (record.path === "/api/jobs" || /^\/api\/jobs\/[A-Za-z0-9_.:-]+\/retry$/.test(record.path));
      })
      .sort((left, right) => String(left.value.created_at ?? "").localeCompare(String(right.value.created_at ?? "")) || left.key.localeCompare(right.key));
  }
  get value() { return this.records[0]?.value ?? null; }

  prepare(path, body) {
    if (typeof body?.request_id !== "string" || !body.request_id) throw new Error("A recovery request requires its original request identifier.");
    if (this.value) throw new Error("A previous request is still being recovered. Wait for its saved result before starting another operation.");
    const record = {path, body, created_at: new Date().toISOString()};
    this.storage.write(`pending-request.${body.request_id}`, record);
    return record;
  }

  async send() {
    // - A second poll must never race the first replay.
    // - Only clear a recovery record on an accepted response or definite 4xx.
    // - A timeout, closed tab or server restart keeps exactly the same body.
    if (this.sending) return null;
    const entry = this.records[0];
    if (!entry) return null;
    const record = entry.value;
    this.sending = true;
    try {
      const job = await this.transport(record.path, {method: "POST", body: record.body});
      if (!job || typeof job.id !== "string" || job.request_id !== record.body.request_id ||
          !["queued", "running", "succeeded", "failed", "interrupted"].includes(job.state)) {
        throw new APIError("The server returned an unexpected job response. The original request is kept for safe recovery.");
      }
      this.storage.remove(entry.key);
      return job;
    } catch (error) {
      if (error instanceof APIError && !error.uncertain) this.storage.remove(entry.key);
      throw error;
    } finally {
      this.sending = false;
    }
  }
}

export function artifactURL(value, origin = location.origin) {
  // - Reject external origins, executable schemes and historical file paths.
  // - Resolve relative API paths against the app origin, never /frontend/.
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin || !/^https?:$/.test(url.protocol) || !url.pathname.startsWith("/api/drafts/")) return null;
    return url.href;
  } catch { return null; }
}

export function downloadFilename(disposition, fallback) {
  // - Honor the server's project-specific attachment name.
  // - Strip path components and control characters even for an unexpected header.
  let value = "";
  const extended = disposition?.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (extended) { try { value = decodeURIComponent(extended[1].trim()); } catch { value = ""; } }
  if (!value) value = disposition?.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i)?.slice(1).find(Boolean)?.trim() ?? fallback;
  value = value.replaceAll("\\", "/").split("/").filter(Boolean).at(-1)?.replace(/[\x00-\x1f\x7f]/g, "").trim();
  return value && value !== "." && value !== ".." ? value : fallback;
}
