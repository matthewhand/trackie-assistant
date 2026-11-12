// Extracts and executes the real keyboard handler with inert event fixtures.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const source = await readFile(process.argv[2], "utf8");
const begin = source.indexOf("  _onKey(e) {");
const end = source.indexOf("\n  _narrow() {", begin);
assert.ok(begin >= 0 && end > begin, "handler boundaries");
const method = source.slice(begin, end);
const Card = new Function("CSS_ESCAPE", "return class SyntheticCard {\n" + method + "\n}")(x => x);
const card = new Card();
card.isConnected = true;
card._focusId = "song-1";
card._activeCategory = "All";
card._sheet = "peek";
card._menu = null;
card._songPage = () => ({ songs: [{ id: "song-1" }] });
card._narrow = () => false;
card.getClientRects = () => [{}];
const effects = [];
const search = { focus() { effects.push("focus-search"); } };
card.shadowRoot = {
  querySelector(selector) { return selector === "#q" ? search : { scrollIntoView() {} }; },
};
card._togglePlay = id => effects.push("play:" + id);
card._toggleExpand = id => effects.push("expand:" + id);
card._render = () => effects.push("render");
card._focusMenuTrigger = (menu) => effects.push("focus-menu:" + menu.kind);
function send(key, path, more = {}) {
  let prevented = false;
  card._onKey({ key, ctrlKey: false, metaKey: false, altKey: false,
    composedPath: () => path, target: path[0],
    preventDefault() { prevented = true; }, ...more });
  return prevented;
}
const outside = { tagName: "BUTTON" };
for (const key of [" ", "Enter", "o", "j", "k", "/", "Escape"]) {
  assert.equal(send(key, [outside, { tagName: "MAIN" }]), false);
}
assert.deepEqual(effects, [], "external widgets must never be hijacked");
for (const key of [" ", "Enter", "o", "/", "j"]) {
  assert.equal(send(key, [{ tagName: "SPAN" }, { tagName: "BUTTON" }, card]), false);
}
assert.equal(send(" ", [{ tagName: "SPAN" }, { tagName: "DIV",
  getAttribute() { return "button"; } }, card]), false);
assert.deepEqual(effects, [], "internal native/ARIA actions must stay native");
assert.equal(send(" ", [{ tagName: "DIV" }, card]), true);
assert.equal(send("Enter", [{ tagName: "DIV" }, card]), true);
assert.equal(send("/", [{ tagName: "DIV" }, card]), true);
assert.deepEqual(effects, ["play:song-1", "expand:song-1", "focus-search"]);
let blurred = false;
card._menu = { kind: "more" };
const input = { tagName: "INPUT", blur() { blurred = true; } };
assert.equal(send("Escape", [input, card]), true);
assert.equal(blurred, true);
assert.equal(card._menu, null);
assert.deepEqual(effects.slice(-2), ["render", "focus-menu:more"]);
console.log("keyboard shortcuts: external focus safe, internal native actions safe, card controls retained");
