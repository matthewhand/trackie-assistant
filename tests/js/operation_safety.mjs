// Execute the real dashboard controller with an inert DOM and intercepted POSTs.
// No browser, application server, filesystem writes, or live API requests.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
const page = readFileSync(process.argv[2], "utf8");
const script = page.split("function esc(s){", 2)[1]
  .split("</script></body></html>", 1)[0];
const elements = new Map();
const doc = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      textContent: "", innerHTML: "", disabled: false, hidden: false,
      checked: false, value: "", dataset: { state: "checking" },
      querySelectorAll() { return []; },
    });
    return elements.get(id);
  },
};
const storage = new Map();
const sessions = {
  getItem(k) { return storage.get(k); },
  setItem(k, v) { storage.set(k, v); },
  removeItem(k) { storage.delete(k); },
};
let confirms = [];
let replies = [];
let requests = [];
const sandbox = {
  document: doc, sessionStorage: sessions, URLSearchParams, Date,
  navigator: {},
  window: { confirm() {
    assert.ok(confirms.length, "unexpected real confirmation");
    return confirms.shift();
  } },
  fetch: async (url, opts) => {
    requests.push({ url, opts });
    assert.ok(replies.length, "unmocked API: " + url);
    const next = replies.shift();
    assert.equal(url, next.url);
    if (next.wait) await next.wait;
    if (next.networkError) throw new Error("synthetic disconnect");
    return { status: next.status, json: async () => next.body };
  },
};
vm.createContext(sandbox);
vm.runInContext("function esc(s){" + script.replace("refresh(); setInterval(refresh,2000);", ""),
  sandbox, { timeout: 1000 });
sandbox.refresh = async () => {}; // never call GET or the real status service
const dry = {dry_run: "1", videos: "0", covers: "1"};
const real = {dry_run: "0", videos: "0", covers: "1"};
let resolve;
const pending = new Promise(r => { resolve = r; });
replies.push({url: "api/sync", status: 200, body: {id: "fictional-0001"}, wait: pending});
const first = sandbox.post("api/sync", dry);
await sandbox.post("api/sync", dry);
assert.equal(requests.length, 1, "rapid clicks sent just one POST");
assert.equal(doc.getElementById("sync-real").disabled, true);
resolve();
await first;
assert.equal(requests.length, 1);
assert.equal(doc.getElementById("sync-dry").disabled, true,
  "accepted operation locks until confirmed in run history");
assert.equal(sandbox.awaitingRun, "fictional-0001");
sandbox.awaitingRun = null;
sandbox.actionLocks();
confirms.push(false);
await sandbox.post("api/sync", real);
assert.equal(requests.length, 1, "cancelled confirmation is not a POST");
confirms.push(true);
replies.push({url: "api/sync", status: 200, body: {id:"fictional-0002"}});
await sandbox.post("api/sync", real);
assert.equal(requests.length, 2);
assert.equal(requests.at(-1).opts.body.get("dry_run"), "0");
sandbox.awaitingRun = null;
sandbox.actionLocks();

// An ambiguous real request stays locked, even after an unsuccessful subsequent tap.
confirms.push(true);
replies.push({url: "api/sync", networkError: true});
await sandbox.post("api/sync", real);
assert.equal(storage.size, 1, "uncertain outcome survives a reload within the tab");
assert.equal(doc.getElementById("sync-real").disabled, true);
assert.equal(doc.getElementById("sync-uncertain").hidden, false);
assert.equal(doc.getElementById("connection-state").dataset.state, "offline");
const before = requests.length;
await sandbox.post("api/sync", real);
assert.equal(requests.length, before, "uncertain real operation never retries");
assert.doesNotMatch(doc.getElementById("opbar").innerHTML, /id="op-retry"/);
doc.getElementById("sync-unlock").onclick();
assert.equal(storage.size, 1, "offline review cannot unlock");
sandbox.pollHealth = "online";
doc.getElementById("sync-unlock").onclick();
assert.equal(storage.size, 0);
assert.equal(doc.getElementById("sync-real").disabled, false);

// A specifically marked producer guard is a definitive no-job response.
confirms.push(true);
replies.push({url: "api/sync", status: 503, body: {
  code: "producer_unavailable", error: "fixture producer verification unavailable"
}});
await sandbox.post("api/sync", real);
assert.equal(storage.size, 0, "producer guard must not leave an uncertain operation lock");
assert.equal(doc.getElementById("sync-real").disabled, false);
assert.equal(doc.getElementById("sync-uncertain").hidden, true);
assert.match(doc.getElementById("opbar").innerHTML, /nothing was started/);
assert.doesNotMatch(doc.getElementById("opbar").innerHTML, /id="op-retry"/);

// A specifically marked tag guard is a definitive no-job response.
confirms.push(true);
replies.push({url: "api/sync", status: 503, body: {
  code: "tagging_unavailable", error: "fixture tagging unavailable"
}});
await sandbox.post("api/sync", real);
assert.equal(storage.size, 0, "tagging guard must not leave an uncertain operation lock");
assert.equal(doc.getElementById("sync-real").disabled, false);
assert.equal(doc.getElementById("sync-uncertain").hidden, true);
assert.match(doc.getElementById("opbar").innerHTML, /nothing was started/);
assert.doesNotMatch(doc.getElementById("opbar").innerHTML, /id="op-retry"/);

// A specifically marked Windows write guard is also a definite no-job response.
confirms.push(true);
replies.push({url: "api/sync", status: 503, body: {
  code: "windows_archive_unavailable", error: "fixture Windows write unavailable"
}});
await sandbox.post("api/sync", real);
assert.equal(storage.size, 0, "Windows guard must not leave an uncertain operation lock");
assert.equal(doc.getElementById("sync-real").disabled, false);
assert.equal(doc.getElementById("sync-uncertain").hidden, true);
assert.match(doc.getElementById("opbar").innerHTML, /nothing was started/);
assert.doesNotMatch(doc.getElementById("opbar").innerHTML, /id="op-retry"/);

// An unmarked 503 remains ambiguous and must not enable another real request.
confirms.push(true);
replies.push({url: "api/sync", status: 503, body: {error: "synthetic server failure"}});
await sandbox.post("api/sync", real);
assert.equal(storage.size, 1, "ordinary 503 must retain the uncertainty lock");
assert.equal(doc.getElementById("sync-real").disabled, true);
assert.equal(doc.getElementById("sync-uncertain").hidden, false);
assert.match(doc.getElementById("opbar").innerHTML, /Outcome unknown/);
assert.doesNotMatch(doc.getElementById("opbar").innerHTML, /id="op-retry"/);
const after503 = requests.length;
await sandbox.post("api/sync", real);
assert.equal(requests.length, after503, "generic 503 must not retry a real request");
sandbox.pollHealth = "online";
doc.getElementById("sync-unlock").onclick();
assert.equal(storage.size, 0);
assert.equal(doc.getElementById("sync-real").disabled, false);

const beforePreview = requests.length;
// Server-side definitive rejection may offer a manual retry for preview ONLY.
replies.push({url: "api/sync", status: 409, body: {message:"fixture busy"}});
await sandbox.post("api/sync", dry);
assert.match(doc.getElementById("opbar").innerHTML, /Retry preview/);
assert.equal(requests.length, beforePreview + 1);
assert.ok(requests.every(x => x.url === "api/sync"), "no live endpoints called");
assert.equal(replies.length, 0);
console.log("operation safety: duplicate guard, confirmation, accepted lock, ambiguous refusal, offline review, preview retry passed");
