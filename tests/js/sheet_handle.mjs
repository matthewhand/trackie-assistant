// Source-only execution of the real mobile sheet renderer and toggle branch.
// No DOM, browser, server, network, or Home Assistant instance.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const code = readFileSync(process.argv[2], "utf8");
const extract = (start, end) => {
  const a = code.indexOf(start), b = code.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, "missing source boundaries: " + start);
  return code.slice(a, b);
};
const paneMethod = extract("  _paneHtml(stats) {", "\n  /** Working panel");
const FakeCard = new Function(
  "classifierMix", "childShares", "fmtStamp", "splitReason",
  "return class FakeCard {\n" + paneMethod + "\n}"
)(
  () => ({ system1: 3, llm: 0, abstained: 0, manual: 0, pinned: 0 }),
  () => [], () => "", () => ""
);
const card = new FakeCard();
card._readOnly = true;
card._genres = { assignments: {}, pinned: [] };
card._hass = null;
card._locked = false;
card._editOpen = false;
card._splitPreview = null;
card._splitRetryable = false;
card._askHtml = () => "";
card._opHtml = () => "";
card._filtered = () => [];
card._esc = value => String(value ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const stats = {
  name: "Synthetic Genre", meta: "fixture", songs: [],
  tracks: 3, pinned: 0, pct: 100, splittable: false,
  parentName: null
};
card._sheet = "peek";
card._render = () => card._paneHtml(stats);
const handle = html => {
  const m = html.match(/<button[^>]*id="sheet-handle"[^>]*><\/button>/);
  assert.ok(m, "sheet handle exists in real render");
  return m[0];
};
const pane = html => {
  assert.equal((html.match(/id="genre-pane-details"/g) || []).length, 1);
  assert.match(html, /class="pfull" id="genre-pane-details"/);
  assert.match(html, /id="pane-edit" disabled/);
  assert.match(html, /id="pane-clear" disabled/);
  assert.match(html, /aria-controls="genre-pane-details"/);
};
let html = card._render();
pane(html);
assert.match(html, /class="pane open peek"/);
assert.match(handle(html), /aria-expanded="false"/);

// Execute the actual switch case, not a rewritten imitation of the handler.
const branch = extract('      case "sheet-handle":', '      case "pane-close":');
const click = new Function("id", "switch (id) {\n" + branch + "\n}");
html = click.call(card, "sheet-handle");
pane(html);
assert.equal(card._sheet, "full");
assert.match(html, /class="pane open full"/);
assert.match(handle(html), /aria-expanded="true"/);
html = click.call(card, "sheet-handle");
assert.equal(card._sheet, "peek");
assert.match(handle(html), /aria-expanded="false"/);

// A completed pointer gesture must suppress its subsequent synthesized click.
card._sheet = "full";
card._dragHandled = true;
click.call(card, "sheet-handle");
assert.equal(card._sheet, "full");
assert.equal(card._dragHandled, false);
console.log("sheet handle: peek/full state, aria-expanded, HA read-only and drag suppression passed");
