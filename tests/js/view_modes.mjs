// Deterministic fake DOM: no browser/server/network or application jobs.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const source = await readFile(process.argv[2], "utf8");
const api = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const { STORAGE_KEY, FEATURE_IDS, normalizePreferences, readPreferences,
  savePreferences, initViewModes, isSinglePanel } = api;

class FakeNode {
  constructor(dataset = {}) {
    this.dataset = { ...dataset };
    this.attributes = {};
    this.listeners = {};
    this.children = [];
    this.hidden = false;
    this.value = "";
    this.scrolled = 0;
    this.focused = 0;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  fire(name, options = {}) { this.listeners[name]?.({ target: this, ...options }); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  contains(target) {
    return this === target || this.children.some((child) => child.contains(target));
  }
  scrollIntoView() { this.scrolled++; }
  focus(options) { this.focused++; this.focusOpts = options; }
}
function fixture(width = 1400) {
  const selectors = new Map();
  for (const id of ["view-mode", "appearance-mode", "density-mode",
    "view-status", "lib"]) selectors.set(id, new FakeNode());
  const buttons = FEATURE_IDS.map((id) => new FakeNode({ section: id }));
  const panels = FEATURE_IDS.map((id) => new FakeNode({ panel: id }));
  const listeners = {};
  const viewListeners = {};
  const defaultView = {
    width,
    matchMedia(query) {
      assert.ok(["(max-width: 1199px)", "(max-width: 700px)"].includes(query));
      return { matches: this.width <= (query.includes("700px") ? 700 : 1199) };
    },
    addEventListener(name, cb) { viewListeners[name] = cb; },
    resize(nextWidth) { this.width = nextWidth; viewListeners.resize(); },
  };
  const doc = {
    body: { dataset: {} }, activeElement: null, defaultView,
    buttons, panels, selectors,
    getElementById: (id) => selectors.get(id),
    querySelectorAll: (sel) => sel === "[data-section]" ? buttons
      : sel === "[data-panel]" ? panels : [],
    addEventListener(name, cb) { listeners[name] = cb; },
    focusOn(node) { this.activeElement = node; listeners.focusin?.({ target: node }); },
  };
  return doc;
}
assert.deepEqual(FEATURE_IDS, ["library", "sync", "recommend", "activity"]);
assert.deepEqual(normalizePreferences(null),
  { view: "classic", theme: "system", density: "comfortable", panel: "library" });
assert.equal(normalizePreferences({ view: "malicious", panel: "bad" }).view, "classic");
assert.equal(normalizePreferences({ view: ["studio"], theme: "light" }).view, "classic");
assert.equal(normalizePreferences({ density: "compact" }).density, "compact");
assert.equal(normalizePreferences({ view: "studio", theme: "contrast" }).theme, "contrast");
assert.equal(isSinglePanel("classic", true), false);
assert.equal(isSinglePanel("classic", true, true), true);
assert.equal(isSinglePanel("studio", false), false);
assert.equal(isSinglePanel("studio", true), true);
assert.equal(isSinglePanel("minimal", false), true);
let saved = "";
const storage = {
  getItem(key) { assert.equal(key, STORAGE_KEY); return saved; },
  setItem(key, value) { assert.equal(key, STORAGE_KEY); saved = value; },
};
assert.equal(readPreferences(storage).view, "classic");
assert.equal(savePreferences(storage, { view: "studio", panel: "sync" }), true);
assert.equal(readPreferences(storage).view, "studio");
assert.equal(readPreferences(storage).panel, "sync");
const broken = { getItem() { throw new Error("denied"); },
  setItem() { throw new Error("denied"); } };
assert.equal(readPreferences(broken).view, "classic");
assert.equal(savePreferences(broken, { view: "minimal" }), false);
saved = "{ broken";
assert.equal(readPreferences(storage).view, "classic");
saved = "";
const doc = fixture();
const libraryNode = doc.getElementById("lib");
libraryNode.setConfig = () => { throw new Error("card must not be reset"); };
const controller = initViewModes(doc, storage);
assert.ok(controller);
assert.equal(controller.libraryElement, libraryNode);
assert.equal(doc.body.dataset.view, "classic");
assert.equal(doc.panels.every((p) => !p.hidden), true);
assert.equal(doc.buttons[0].attributes["aria-pressed"], undefined);
assert.ok(Object.hasOwn(libraryNode.attributes, "data-suno-theme"));
doc.getElementById("view-mode").value = "studio";
doc.getElementById("view-mode").fire("change");
assert.equal(doc.body.dataset.view, "studio");
assert.equal(doc.panels.every((p) => !p.hidden), true);
assert.equal(doc.getElementById("lib"), libraryNode);
doc.buttons[2].fire("click", { detail: 0 });
assert.equal(controller.preferences.panel, "recommend");
assert.equal(doc.panels[2].dataset.active, "true");
assert.equal(doc.panels[2].focused, 1); // keyboard user arrives at selected region
assert.equal(doc.panels[2].scrolled, 1);
doc.getElementById("appearance-mode").value = "light";
doc.getElementById("appearance-mode").fire("change");
assert.equal(doc.body.dataset.theme, "light");
doc.getElementById("density-mode").value = "compact";
doc.getElementById("density-mode").fire("change");
assert.ok(Object.hasOwn(libraryNode.attributes, "data-compact"));
doc.getElementById("density-mode").value = "comfortable";
doc.getElementById("density-mode").fire("change");
assert.equal(libraryNode.attributes["data-compact"], undefined);
doc.getElementById("view-mode").value = "minimal";
doc.getElementById("view-mode").fire("change");
assert.equal(doc.body.dataset.view, "minimal");
assert.equal(doc.body.dataset.theme, "light");
assert.equal(doc.panels.filter((p) => !p.hidden).length, 1);
assert.equal(doc.panels[2].hidden, false);
assert.equal(doc.buttons[2].attributes["aria-pressed"], "true");
assert.equal(doc.buttons[0].attributes["aria-pressed"], "false");
assert.equal(doc.getElementById("view-status").textContent, "minimal view, recommend section.");
doc.buttons[1].fire("click", { detail: 1 });
assert.equal(controller.preferences.panel, "sync");
assert.equal(doc.panels[1].hidden, false);
assert.equal(doc.panels[2].hidden, true);
assert.equal(doc.panels[1].focused, 0); // pointer should keep focus on the button
assert.equal(doc.getElementById("lib"), libraryNode);
const restoredDoc = fixture();
const restored = initViewModes(restoredDoc, storage);
assert.equal(restored.preferences.view, "minimal");
assert.equal(restored.preferences.panel, "sync");
assert.equal(restoredDoc.panels[1].hidden, false);
assert.equal(restoredDoc.getElementById("lib"), restoredDoc.selectors.get("lib"));

// When a viewport shrinks, the pane holding focus becomes selected (not hidden).
saved = JSON.stringify({ view: "studio", theme: "system", density: "comfortable", panel: "library" });
const resizeDoc = fixture(1400);
const resized = initViewModes(resizeDoc, storage);
const syncInput = new FakeNode();
resizeDoc.panels[1].children.push(syncInput);
resizeDoc.focusOn(syncInput);
resizeDoc.defaultView.resize(1024);
assert.equal(resized.preferences.panel, "sync");
assert.equal(resizeDoc.panels[1].hidden, false);
assert.equal(resizeDoc.panels[0].hidden, true);
assert.equal(resizeDoc.activeElement, syncInput);
assert.equal(resizeDoc.buttons[1].attributes["aria-pressed"], "true");
resizeDoc.defaultView.resize(1400);
assert.ok(resizeDoc.panels.every((p) => !p.hidden));
assert.equal(resizeDoc.buttons[1].attributes["aria-pressed"], undefined);
assert.equal(resizeDoc.activeElement, syncInput);

// Switching via a header select honors the last interacted-with feature.
resizeDoc.getElementById("view-mode").value = "minimal";
resizeDoc.focusOn(resizeDoc.getElementById("view-mode"));
resizeDoc.getElementById("view-mode").fire("change");
assert.equal(resized.preferences.panel, "sync");
assert.equal(resizeDoc.panels[1].hidden, false);
assert.equal(resizeDoc.activeElement, resizeDoc.getElementById("view-mode"));
// Browsers may blur a soon-to-be-hidden panel before dispatching resize.
saved = JSON.stringify({ view: "studio", theme: "system", density: "comfortable", panel: "library" });
const blurDoc = fixture(1400);
const blurController = initViewModes(blurDoc, storage);
const recommendInput = new FakeNode();
blurDoc.panels[2].children.push(recommendInput);
blurDoc.focusOn(recommendInput);
// Actual browser regression: CSS hides the input before the resize callback
// can select and reveal its panel. Restore the control, not just the panel.
recommendInput.focus = function (options) {
  this.focused++;
  this.focusOpts = options;
  blurDoc.focusOn(this);
};
blurDoc.activeElement = blurDoc.body; // browser blur raced with media-query repaint
blurDoc.defaultView.resize(768);
assert.equal(blurController.preferences.panel, "recommend");
assert.equal(blurDoc.panels[2].hidden, false);
assert.equal(blurDoc.panels[0].hidden, true);
assert.equal(blurDoc.activeElement, recommendInput);
assert.equal(recommendInput.focused, 1);
assert.deepEqual(recommendInput.focusOpts, { preventScroll: true });

// A still-focused input must not be focused a second time during a resize.
saved = JSON.stringify({ view: "studio", panel: "library" });
const retainedDoc = fixture(1440);
initViewModes(retainedDoc, storage);
const retainedInput = new FakeNode();
retainedDoc.panels[1].children.push(retainedInput);
retainedDoc.focusOn(retainedInput);
retainedDoc.defaultView.resize(768);
assert.equal(retainedDoc.activeElement, retainedInput);
assert.equal(retainedInput.focused, 0);

// External focus (including the mode selector) must never be stolen.
saved = JSON.stringify({ view: "studio", panel: "library" });
const externalDoc = fixture(1440);
const external = initViewModes(externalDoc, storage);
const earlierInput = new FakeNode();
externalDoc.panels[2].children.push(earlierInput);
externalDoc.focusOn(earlierInput);
const viewSelect = externalDoc.getElementById("view-mode");
externalDoc.focusOn(viewSelect);
externalDoc.defaultView.resize(768);
assert.equal(external.preferences.panel, "recommend");
assert.equal(externalDoc.activeElement, viewSelect);
assert.equal(earlierInput.focused, 0);

// A view-menu change is not a viewport-collapse event: never steal focus.
saved = JSON.stringify({ view: "classic", panel: "library" });
const modeDoc = fixture(1440);
initViewModes(modeDoc, storage);
const modeInput = new FakeNode();
modeDoc.panels[2].children.push(modeInput);
modeDoc.focusOn(modeInput);
modeDoc.activeElement = modeDoc.body;
modeDoc.getElementById("view-mode").value = "minimal";
modeDoc.getElementById("view-mode").fire("change");
assert.equal(modeDoc.activeElement, modeDoc.body);
assert.equal(modeDoc.panels[2].hidden, false);
assert.equal(modeInput.focused, 0);

// A stale/disconnected input is not a safe focus-restoration target.
saved = JSON.stringify({ view: "studio", panel: "library" });
const staleDoc = fixture(1440);
initViewModes(staleDoc, storage);
const staleInput = new FakeNode();
staleDoc.panels[2].children.push(staleInput);
staleDoc.focusOn(staleInput);
staleInput.isConnected = false;
staleDoc.activeElement = staleDoc.body;
staleDoc.defaultView.resize(768);
assert.equal(staleInput.focused, 0);
assert.equal(initViewModes({ body: {}, getElementById: () => null,
  querySelectorAll: () => [] }), null);
console.log("view-mode synthetic DOM: accessibility, focus, resize, persistence and node identity passed");

// Classic mobile offers direct feature tabs while the wide Classic keeps all panels.
saved = JSON.stringify({ view: "classic", theme: "system", density: "comfortable", panel: "library" });
const classicMobile = fixture(390);
const mobileController = initViewModes(classicMobile, storage);
assert.equal(classicMobile.panels.filter(p => !p.hidden).length, 1);
classicMobile.buttons[3].fire("click", { detail: 1 });
assert.equal(mobileController.preferences.panel, "activity");
assert.equal(classicMobile.panels[3].hidden, false);
assert.equal(classicMobile.buttons[3].attributes["aria-pressed"], "true");
classicMobile.defaultView.resize(768);
assert.ok(classicMobile.panels.every(p => !p.hidden));
classicMobile.defaultView.resize(320);
assert.equal(classicMobile.panels[3].hidden, false);
console.log("classic mobile: navigation, state preservation and portrait/tablet transition passed");
