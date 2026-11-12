// Source-only dashboard regression: execute actual inline JS with inert DOM and fetch stubs.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const page = await readFile(process.argv[2], "utf8");
const start = page.indexOf("function esc(s){");
const end = page.indexOf("</script></body></html>", start);
assert.ok(start >= 0 && end > start, "inline dashboard script found");
const code = page.slice(start, end);
const boot = "refresh(); setInterval(refresh,2000);";
assert.ok(code.includes(boot), "polling startup isolated");
assert.ok(page.includes('<div id="opbar"></div>'), "visible banner is not a live region");
assert.ok(page.includes('id="op-announcement" class="sr-only" role="status"'),
  "dedicated announcer exists outside polling banner");
assert.doesNotMatch(page, /<div id="opbar" aria-live=/);
const script = code.replace(boot, ""); // no timers or initial network requests

const elements = new Map();
function genericNode() {
  return { textContent: "", innerHTML: "", disabled: false, checked: false,
    value: "", dataset: {state:"checking"}, hidden: false, children: [], appendChild(n) { this.children.push(n); },
    append(n) { this.children.push(n); }, querySelectorAll() { return []; },
    replaceChildren(...nodes) { this.children = nodes; },
    classList: { add() {}, remove() {} } };
}
class Bar {
  constructor() { this.writes = 0; this._html = ""; }
  set innerHTML(value) {
    this.writes++;
    this._html = value;
    this.line = value.includes('id="op-line"') ? { textContent: "" } : null;
    this.cancel = value.includes('id="op-cancel"') ? { disabled: false } : null;
    const fill = { style: { width: "0%" } };
    this.progress = value.includes('id="op-bar"')
      ? { hidden: value.includes(" hidden"), attrs: {}, fill,
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
        querySelector: (k) => k === "i" ? fill : null } : null;
    const stale = value.includes('data-stale="true"');
    this.panel = value.includes('class="op"') ? {
      attrs: { "data-stale": stale ? "true" : "false" },
      setAttribute(k, v) { this.attrs[k] = v; },
    } : null;
    this.stale = value.includes('id="op-stale"')
      ? { hidden: !stale, textContent: "Connection unavailable; last confirmed progress shown. Retrying." }
      : null;
  }
  get innerHTML() { return this._html; }
  querySelector(k) {
    return ({ "#op-line": this.line, "#op-cancel": this.cancel,
      "#op-bar": this.progress, "#op-stale": this.stale,
      ".op": this.panel })[k] || null;
  }
}
const bar = new Bar();
const announcements = [];
const announcer = {
  _text: "",
  get textContent() { return this._text; },
  set textContent(value) { this._text = value; announcements.push(value); },
};
const doc = { activeElement: null,
  dispatchEvent(event) {
    assert.equal(event.type, "trackie-sync-failures");
    assert.ok(Array.isArray(event.detail.failures));
  },
  createElement() { return genericNode(); },
  getElementById(id) {
    if (id === "opbar") return bar;
    if (id === "op-announcement") return announcer;
    if (!elements.has(id)) elements.set(id, genericNode());
    return elements.get(id);
  } };
let replies = [];
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
const sandbox = {
  document: doc, URLSearchParams, Date, navigator: {},
  CustomEvent: class {
    constructor(type, { detail }) { this.type = type; this.detail = detail; }
  },
  setInterval() { throw Error("timer must not start in synthetic fixture"); },
  fetch: async (url) => {
    assert.ok(replies.length, "unmocked network call: " + url);
    const reply = replies.shift();
    assert.equal(url, reply.url);
    if (reply.seen) reply.seen();
    if (reply.wait) await reply.wait;
    if (reply.error) throw Error("offline");
    return { status: reply.status ?? 200, json: async () => reply.body };
  } };
vm.createContext(sandbox);
vm.runInContext(script, sandbox, { timeout: 1000 });
const enqueue = (...items) => { replies.push(...items); };
const status = { version: "synthetic", archive: "fixture", engine: "fake",
  watch: [], blocked: { count: 0 }, profiles: [] };

enqueue({ url: "api/status", error: true });
await sandbox.refresh();
assert.match(elements.get("status").textContent, /Status unavailable/);
assert.match(elements.get("runs").textContent, /stale/);
assert.equal(bar.writes, 0, "offline with no confirmed run cannot invent a progress banner");
assert.equal(announcements.length, 1, "initial offline announced once");
assert.match(announcer.textContent, /Connection unavailable/);

enqueue({ url: "api/status", body: { version: "broken" } });
await sandbox.refresh();
assert.match(elements.get("status").textContent, /invalid response/);
assert.equal(announcements.length, 1, "repeated malformed polls are silent");

enqueue({ url: "api/status", body: status },
  { url: "api/runs", status: 503, body: { code: "offline" } });
await sandbox.refresh();
assert.match(elements.get("runs").textContent, /Run history unavailable/);

enqueue({ url: "api/status", body: status },
  { url: "api/runs", body: { runs: [] } });
await sandbox.refresh();
assert.equal(elements.get("runs").textContent, "no runs yet");
assert.equal(announcements.length, 2, "recovery announced once");
assert.match(announcer.textContent, /Connection restored/);

sandbox.setBar({ kind: "running", runId: "synthetic-run", line: "First step",
  cancellable: true, progress: { done: 1, total: 10 } });
const cancel = bar.querySelector("#op-cancel");
const progress = bar.querySelector("#op-bar");
const writes = bar.writes;
doc.activeElement = cancel;
sandbox.setBar({ kind: "running", runId: "synthetic-run", line: "Second step",
  cancellable: true, progress: { done: 4, total: 10 } });
assert.equal(bar.writes, writes, "poll update must not replace active controls");
assert.equal(bar.querySelector("#op-cancel"), cancel, "Cancel node identity stable");
assert.equal(doc.activeElement, cancel, "keyboard focus remains on Cancel");
assert.equal(bar.querySelector("#op-line").textContent, "Second step");
assert.equal(progress.attrs["aria-valuenow"], "40");
assert.equal(progress.fill.style.width, "40%");
assert.equal(bar.querySelector("#op-stale").hidden, true);
assert.equal(progress.attrs["aria-label"], "Run progress");
sandbox.setBar({ kind: "running", runId: "synthetic-run", line: "Second step",
  cancellable: true, progress: { done: 4, total: 10 }, stale: true });
assert.equal(progress.attrs["aria-valuenow"], "40", "known percentage remains visible");
assert.equal(progress.attrs["aria-label"], "Last confirmed progress");
assert.match(bar.querySelector("#op-line").textContent, /^Last confirmed:/);
assert.equal(bar.querySelector("#op-stale").hidden, false);
assert.equal(doc.activeElement, cancel);
assert.equal(bar.writes, writes, "stale presentation preserves Cancel element");
sandbox.setBar({ kind: "running", runId: "synthetic-run", line: "Second step",
  cancellable: true, progress: { done: 4, total: 10 } });
assert.equal(bar.querySelector("#op-stale").hidden, true);
assert.equal(progress.attrs["aria-label"], "Run progress");
sandbox.setBar({ kind: "running", runId: "synthetic-run", line: "Unknown",
  cancellable: true, progress: null });
assert.equal(progress.hidden, true);
assert.equal(progress.attrs["aria-valuenow"], undefined);
sandbox.setBar({ kind: "running", runId: "synthetic-run", line: "Cancelling",
  cancellable: true, cancelling: true, progress: null });
assert.equal(cancel.disabled, true);
assert.equal(bar.writes, writes);

// A failed refresh must keep the last confirmed run and the same focused
// Cancel control. The banner must make both progress and connectivity explicit.
enqueue({ url: "api/status", error: true });
await sandbox.refresh();
assert.equal(bar.writes, writes, "offline polling must keep the banner DOM");
assert.equal(bar.querySelector("#op-cancel"), cancel);
assert.equal(doc.activeElement, cancel);
assert.equal(cancel.disabled, true, "cancelling stays disabled while offline");
assert.equal(bar.querySelector("#op-stale").hidden, false);
assert.equal(bar.querySelector(".op").attrs["data-stale"], "true");
assert.match(bar.querySelector("#op-line").textContent, /^Last confirmed:/);
assert.equal(progress.attrs["aria-label"], "Last confirmed progress");
assert.equal(progress.hidden, true, "unknown progress remains unknown");
assert.equal(progress.attrs["aria-valuenow"], undefined);
assert.equal(announcements.length, 3, "connection loss announced once");
enqueue({ url: "api/status", body: { version: "malformed" } });
await sandbox.refresh();
enqueue({ url: "api/status", body: status },
  { url: "api/runs", status: 503, body: { error: "unavailable" } });
await sandbox.refresh();
assert.equal(bar.writes, writes);
assert.equal(announcements.length, 3, "repeat offline/malformed polls are quiet");

// A validated run response is the sole authority for clearing stale state.
const live = progressValue => ({
  id: "synthetic-run", kind: "sync", state: "running",
  progress: progressValue,
  output: [],
});
enqueue({ url: "api/status", body: status },
  { url: "api/runs", body: { runs: [live({ done: 6, total: 10 })] } });
await sandbox.refresh();
assert.equal(bar.querySelector("#op-cancel"), cancel);
assert.equal(doc.activeElement, cancel);
assert.equal(bar.querySelector("#op-stale").hidden, true);
assert.equal(bar.querySelector(".op").attrs["data-stale"], "false");
assert.equal(progress.attrs["aria-label"], "Run progress");
assert.equal(progress.attrs["aria-valuenow"], "60");
assert.equal(cancel.disabled, false, "fresh confirmed running state re-enables Cancel");
assert.equal(bar.writes, writes);
assert.equal(announcements.length, 4, "recovery announces once");

// A fresh confirmed cancelling run is disabled, including after losing status.
enqueue({ url: "api/status", body: status },
  { url: "api/runs", body: { runs: [{ ...live(null), state: "cancelling" }] } });
await sandbox.refresh();
assert.equal(cancel.disabled, true);
assert.equal(progress.hidden, true);
enqueue({ url: "api/status", body: status },
  { url: "api/runs", error: true });
await sandbox.refresh();
assert.equal(cancel.disabled, true);
assert.equal(progress.attrs["aria-valuenow"], undefined);
assert.equal(bar.querySelector("#op-stale").hidden, false);
assert.equal(bar.writes, writes);

// Delayed responses from earlier polls must not override a newer confirmed run.
const previousReply = deferred();
const started = deferred();
enqueue({ url: "api/status", seen: () => started.resolve(),
  wait: previousReply.promise, error: true });
const oldPoll = sandbox.refresh();
await started.promise;
enqueue({ url: "api/status", body: status },
  { url: "api/runs", body: { runs: [live({ done: 8, total: 10 })] } });
await sandbox.refresh();
previousReply.resolve();
await oldPoll;
assert.equal(progress.attrs["aria-valuenow"], "80");
assert.equal(bar.querySelector("#op-stale").hidden, true);
assert.equal(bar.querySelector("#op-cancel"), cancel);

// Also reject an older run-history response arriving after a newer one.
const oldHistory = deferred();
const historyStarted = deferred();
enqueue({ url: "api/status", body: status },
  { url: "api/runs", seen: () => historyStarted.resolve(),
    wait: oldHistory.promise, body: { runs: [live({ done: 1, total: 10 })] } });
const oldRunPoll = sandbox.refresh();
await historyStarted.promise;
enqueue({ url: "api/status", body: status },
  { url: "api/runs", body: { runs: [live({ done: 9, total: 10 })] } });
await sandbox.refresh();
oldHistory.resolve();
await oldRunPoll;
assert.equal(progress.attrs["aria-valuenow"], "90");
assert.equal(bar.querySelector("#op-cancel"), cancel);
assert.equal(bar.writes, writes, "newer and older responses cannot replace Cancel");

// A slow completed poll remains useful even while the next 2-second poll is
// outstanding; a never-ending stream of requests must not starve progress.
const slowStatus = deferred();
const slowSeen = deferred();
enqueue({ url: "api/status", seen: () => slowSeen.resolve(), wait: slowStatus.promise,
  body: status });
const slowPoll = sandbox.refresh();
await slowSeen.promise;
const nextStatus = deferred();
const nextSeen = deferred();
enqueue({ url: "api/status", seen: () => nextSeen.resolve(), wait: nextStatus.promise,
  body: status });
const nextPoll = sandbox.refresh();
await nextSeen.promise;
enqueue({ url: "api/runs", body: { runs: [live({ done: 95, total: 100 })] } });
slowStatus.resolve();
await slowPoll;
assert.equal(progress.attrs["aria-valuenow"], "95", "latest settled poll stays useful");
enqueue({ url: "api/runs", body: { runs: [live({ done: 96, total: 100 })] } });
nextStatus.resolve();
await nextPoll;
assert.equal(progress.attrs["aria-valuenow"], "96", "newer result replaces earlier result");
assert.equal(bar.querySelector("#op-cancel"), cancel);
assert.equal(replies.length, 0, "all synthetic responses consumed");
console.log("dashboard resilience: offline/malformed status, run history and operation focus passed");
console.log("stale status: last-known progress, stable Cancel, quiet announcements, recovery, races passed");
