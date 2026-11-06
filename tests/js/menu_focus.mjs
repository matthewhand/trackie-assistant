// Source-only execution of real delegated menu and keyboard handlers; no DOM or browser.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const source = await readFile(process.argv[2], "utf8");
function method(from, to) {
  const a = source.indexOf(from);
  const b = source.indexOf(to, a);
  assert.ok(a >= 0 && b > a, "method boundaries: " + from);
  return source.slice(a, b);
}
const helpers = method("  _focusMenuTrigger(menu) {", "\n  _wire() {");
const click = method("  _onClick(e) {", "\n  async _onButton(");
const key = method("  _onKey(e) {", "\n  _narrow() {");
const copy = method("  async _copySong(clip, kind, button) {", "\n  async _togglePin(");
const pin = method("  async _togglePin(songId) {", "\n  async _clearCategory(");
const Card = new Function("CSS_ESCAPE", "recount", "return class SyntheticCard {\n" +
  helpers + "\n" + click + "\n" + key + "\n" + copy + "\n" + pin + "\n}")(x => x, x => x);
const card = new Card();
const observed = [];
let menuFocus = 0;
let triggerFocus = 0;
const trigger = { getAttribute(name) { return name === "data-menu-more" ? "song-1" : null; },
  focus() { triggerFocus++; } };
card.shadowRoot = {
  querySelector(sel) {
    if (sel === ".menu button:not([disabled])") return { focus() { menuFocus++; } };
    return null;
  },
  querySelectorAll(sel) { return sel === "[data-menu-more]" ? [trigger] : []; },
};
card.isConnected = true;
card.getClientRects = () => [{}];
card._activeCategory = "All";
card._sheet = "peek";
card._menu = null;
card._render = () => observed.push("render");
function target(attrs, inMenu = false) {
  return {
    tagName: "BUTTON",
    closest(sel) {
      if (sel === "button") return this;
      if (sel === ".menu") return inMenu ? this : null;
      return Object.hasOwn(attrs, sel.slice(1, -1)) ? this : null;
    },
    getAttribute(name) { return attrs[name] ?? null; },
    hasAttribute(name) { return Object.hasOwn(attrs, name); },
  };
}
card._onClick({ target: target({ "data-menu-more": "song-1" }) });
assert.equal(card._menu.kind, "more");
assert.equal(menuFocus, 1, "opening menu focuses first enabled action");
card._onClick({ target: target({ "data-menu-move": "song-1" }, true) });
assert.equal(card._menu.sub, "move");
assert.equal(menuFocus, 2, "submenu entry gains focus");
card._onClick({ target: target({ "data-menu-back": "1" }, true) });
assert.equal(card._menu.sub, null);
assert.equal(menuFocus, 3, "submenu Back restores action focus");
let prevented = 0;
card._onKey({ key: "Escape", ctrlKey: false, metaKey: false, altKey: false,
  target: { tagName: "BUTTON" },
  composedPath: () => [{ tagName: "BUTTON" }, card],
  preventDefault() { prevented++; } });
assert.equal(card._menu, null);
assert.equal(prevented, 1);
assert.equal(triggerFocus, 1, "Escape focuses original menu trigger");
assert.equal(observed.length, 4);
card._fetchLyrics = async () => {};
card._lyricsFor = (clip) => clip.lyrics || "";
card._copy = async () => card._render();
card._menu = { id: "song-1", kind: "more", sub: null };
await card._copySong({ id: "song-1", title: "Synthetic song",
  style: "Synthetic", lyrics: "" }, "title", null);
assert.equal(triggerFocus, 2, "Copy returns focus after asynchronous completion");
card._moveSong = async () => { observed.push("move"); };
card._menu = { id: "song-1", kind: "more", sub: "move" };
await card._onClick({ target: target({ "data-move-to": "Fixture", "data-song": "song-1" }, true) });
assert.equal(triggerFocus, 3, "Move returns focus after asynchronous completion");
card._readOnly = false;
card._genres = { pinned: [] };
card._save = async () => observed.push("save");
card._menu = { id: "song-1", kind: "more", sub: null };
await card._togglePin("song-1");
assert.equal(triggerFocus, 4, "Pin returns focus after asynchronous completion");
// Run the actual HTML generators with synthetic-only song data. The controls
// are native buttons in an explicitly labelled group (not an ARIA menu).
const markup = method("  _menuHtml(c) {", "\n  _askHtml(count) {");
const escapeMethod = method("  _esc(s) {", "\n  _playState(c) {");
const MarkupCard = new Function("leafNames", "parentMap", "shortChildName",
  "parseBpm", "fmtDuration", "fmtDate", "PAUSE_SVG", "PLAY_SVG",
  "return class MarkupCard {\n" + escapeMethod + "\n" + markup + "\n}")(
  () => ["Synthetic Genre"], () => ({}), (_, name) => name,
  () => null, () => "", () => "", "", "");
const cardMarkup = new MarkupCard();
cardMarkup._genres = { assignments: {}, pinned: [] };
cardMarkup._tracks = {};
cardMarkup._expanded = new Set();
cardMarkup._focusId = null;
cardMarkup._playing = null;
cardMarkup._readOnly = true;
cardMarkup._locked = false;
cardMarkup._playState = () => ({ off: true, reason: "Synthetic audio unavailable" });
cardMarkup._isPlaying = () => false;
const song = { id: "song-1", title: 'Synth "One" & <Two>', style: "Synthetic" };
const label = "Synth &quot;One&quot; &amp; &lt;Two&gt;";
const buttonFor = (html, attr) => {
  const found = html.match(new RegExp('<button\\b[^>]*\\b' + attr + '="song-1"[^>]*>'));
  assert.ok(found, "missing " + attr + " trigger");
  return found[0];
};
const closedHtml = cardMarkup._cardHtml(song, new Set());
assert.doesNotMatch(closedHtml, /role="menu(?:item)?"/);
assert.doesNotMatch(closedHtml, /id="track-action-group"/);
for (const attr of ["data-menu-copy", "data-menu-more"]) {
  assert.match(buttonFor(closedHtml, attr), /aria-expanded="false"/);
  assert.doesNotMatch(buttonFor(closedHtml, attr), /aria-controls=/);
}

cardMarkup._menu = { id: song.id, kind: "copy", sub: null };
const copyHtml = cardMarkup._cardHtml(song, new Set());
assert.match(copyHtml, new RegExp('id="track-action-group" role="group" aria-label="Copy options for ' + label + '"'));
assert.doesNotMatch(copyHtml, /role="menu(?:item)?"/);
assert.match(buttonFor(copyHtml, "data-menu-copy"), /aria-expanded="true"/);
assert.match(buttonFor(copyHtml, "data-menu-copy"), /aria-controls="track-action-group"/);
assert.match(buttonFor(copyHtml, "data-menu-more"), /aria-expanded="false"/);
assert.doesNotMatch(buttonFor(copyHtml, "data-menu-more"), /aria-controls=/);
assert.equal((copyHtml.match(/id="track-action-group"/g) || []).length, 1);
assert.equal((copyHtml.match(/data-copy-kind=/g) || []).length, 4);
assert.match(copyHtml, /<button type="button" data-copy-kind="title"/);

cardMarkup._menu = { id: song.id, kind: "more", sub: null };
const readOnlyMore = cardMarkup._cardHtml(song, new Set());
assert.match(readOnlyMore, new RegExp('role="group" aria-label="More actions for ' + label + '"'));
assert.match(buttonFor(readOnlyMore, "data-menu-more"), /aria-expanded="true"/);
assert.match(buttonFor(readOnlyMore, "data-menu-more"), /aria-controls="track-action-group"/);
assert.match(buttonFor(readOnlyMore, "data-menu-copy"), /aria-expanded="false"/);
assert.match(readOnlyMore, /data-menu-move="song-1" disabled/);
assert.match(readOnlyMore, /data-pin="song-1" disabled/);
assert.match(readOnlyMore, /Read-only here/);

cardMarkup._readOnly = false; // Existing Home Assistant editing contract.
const editableMore = cardMarkup._cardHtml(song, new Set());
assert.doesNotMatch(editableMore, /data-menu-move="song-1" disabled/);
assert.doesNotMatch(editableMore, /data-pin="song-1" disabled/);
assert.doesNotMatch(editableMore, /Read-only here/);
cardMarkup._menu = { id: song.id, kind: "more", sub: "move" };
const moveHtml = cardMarkup._cardHtml(song, new Set());
assert.match(moveHtml, new RegExp('role="group" aria-label="Move to genre for ' + label + '"'));
assert.match(moveHtml, /data-menu-back="1"/);
assert.match(moveHtml, /data-move-to="Synthetic Genre"/);
assert.match(buttonFor(moveHtml, "data-menu-more"), /aria-expanded="true"/);
assert.doesNotMatch(moveHtml, /role="menu(?:item)?"/);
console.log("menu focus: open, submenu, Back, Escape, Copy, Move and Pin passed");
console.log("menu semantics: native buttons, group labels, expanded state and HA permissions passed");
