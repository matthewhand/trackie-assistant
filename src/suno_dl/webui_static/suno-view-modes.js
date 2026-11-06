/** Three view layouts, independent appearance/density, one shared live dashboard DOM.
 * Browser preferences contain presentation only; no credentials, library data, or URLs.
 */
export const STORAGE_KEY = "suno.webui.preferences.v1";
export const DEFAULT_PREFERENCES = Object.freeze({
  view: "classic", theme: "system", density: "comfortable", panel: "library",
});
export const FEATURE_IDS = Object.freeze(["library", "sync", "recommend", "activity"]);
const ALLOWED = Object.freeze({
  view: ["classic", "studio", "minimal"],
  theme: ["system", "dark", "light", "contrast"],
  density: ["comfortable", "compact"],
  panel: FEATURE_IDS,
});

export function normalizePreferences(candidate) {
  const value = candidate && typeof candidate === "object" && !Array.isArray(candidate)
    ? candidate : {};
  const result = {};
  for (const key of Object.keys(DEFAULT_PREFERENCES)) {
    const input = value[key];
    result[key] = typeof input === "string" && ALLOWED[key].includes(input)
      ? input : DEFAULT_PREFERENCES[key];
  }
  return result;
}

export function readPreferences(storage) {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    return normalizePreferences(raw ? JSON.parse(raw) : null);
  } catch (_) {
    return normalizePreferences(null);
  }
}

export function savePreferences(storage, preferences) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(normalizePreferences(preferences)));
    return true;
  } catch (_) {
    return false; // private browsing, security errors, quota, or storage disabled
  }
}

/** Narrow Studio and Minimal show one panel; wide Studio and Classic show all. */
export function isSinglePanel(view, compactWorkspace, mobileWorkspace = false) {
  return view === "minimal" || (view === "studio" && !!compactWorkspace)
    || (view === "classic" && !!mobileWorkspace);
}

/** Switch CSS presentation, never replace the library component or its audio. */
export function initViewModes(doc, storage) {
  if (!doc?.body) return null;
  const picks = {
    view: doc.getElementById("view-mode"),
    theme: doc.getElementById("appearance-mode"),
    density: doc.getElementById("density-mode"),
  };
  const buttons = [...doc.querySelectorAll("[data-section]")];
  const panels = [...doc.querySelectorAll("[data-panel]")];
  const status = doc.getElementById("view-status");
  const lib = doc.getElementById("lib");
  if (!lib || buttons.length !== FEATURE_IDS.length || panels.length !== FEATURE_IDS.length
    || Object.values(picks).some((item) => !item)) return null;

  let settings = readPreferences(storage);
  let lastFocusedPanel = null;
  let lastFocusedElement = null;
  const narrow = () => !!doc.defaultView?.matchMedia?.("(max-width: 1199px)").matches;
  const mobile = () => !!doc.defaultView?.matchMedia?.("(max-width: 700px)").matches;
  const single = () => isSinglePanel(settings.view, narrow(), mobile());
  const focusedPanel = () => panels.find((panel) =>
    panel === doc.activeElement || panel.contains?.(doc.activeElement));
  let previouslySingle = single();

  function update({ announce = false, retainFocusedPanel = false, restoreBlurredFocus = false } = {}) {
    const collapsing = restoreBlurredFocus && single() && !previouslySingle;
    // When resizing from a wide Studio workspace to tabs, keep the task that
    // contains keyboard focus visible instead of hiding the active control.
    if (retainFocusedPanel && single()) {
      const active = focusedPanel();
      if (active) settings.panel = active.dataset.panel;
      else if (!previouslySingle && lastFocusedPanel) settings.panel = lastFocusedPanel;
    }
    doc.body.dataset.view = settings.view;
    doc.body.dataset.theme = settings.theme;
    doc.body.dataset.density = settings.density;
    lib.setAttribute("data-suno-theme", "");
    if (settings.density === "compact") lib.setAttribute("data-compact", "");
    else lib.removeAttribute("data-compact");
    for (const key of Object.keys(picks)) picks[key].value = settings[key];
    const exclusive = single();
    for (const panel of panels) {
      const visible = !exclusive || panel.dataset.panel === settings.panel;
      panel.dataset.active = String(panel.dataset.panel === settings.panel);
      panel.hidden = !visible;
    }
    for (const button of buttons) {
      if (exclusive) {
        button.setAttribute("aria-pressed", String(button.dataset.section === settings.panel));
      } else {
        button.removeAttribute("aria-pressed");
      }
    }
    // CSS may blur a focused input before the resize event runs. Once its
    // panel is visible again, restore focus only when the document lost it.
    // Do not override a new focus target, including header controls.
    if (collapsing && (!doc.activeElement || doc.activeElement === doc.body
        || doc.activeElement === doc.documentElement)) {
      const panel = panels.find((item) => item.dataset.panel === settings.panel);
      if (panel?.contains?.(lastFocusedElement)
          && lastFocusedElement?.isConnected !== false
          && !lastFocusedElement?.disabled && lastFocusedElement?.focus) {
        lastFocusedElement.focus({ preventScroll: true });
      }
    }
    if (announce && status) {
      status.textContent = settings.view + " view, " + settings.panel + " section.";
    }
    previouslySingle = exclusive;
  }

  function change(key, next) {
    const wasSingle = single();
    settings = normalizePreferences({ ...settings, [key]: next });
    // The view selector lives outside the panels, so preserve recent context
    // when switching from an all-panels layout into an exclusive layout.
    if (key === "view" && !wasSingle && single() && lastFocusedPanel) {
      settings.panel = lastFocusedPanel;
    }
    update({ announce: true, retainFocusedPanel: key === "view" });
    savePreferences(storage, settings);
    return { ...settings };
  }

  for (const key of Object.keys(picks)) {
    picks[key].addEventListener("change", () => change(key, picks[key].value));
  }
  for (const button of buttons) {
    button.addEventListener("click", (event) => {
      const next = button.dataset.section;
      change("panel", next);
      const section = panels.find((p) => p.dataset.panel === next);
      // Keyboard activation moves to the panel heading region. Pointer users
      // retain focus on navigation to avoid unsolicited focus jumps.
      if (event?.detail === 0 && section?.focus) section.focus({ preventScroll: true });
      if (section?.scrollIntoView) section.scrollIntoView({ block: "start" });
    });
  }
  if (doc.addEventListener) {
    doc.addEventListener("focusin", (event) => {
      const panel = panels.find((item) =>
        item === event.target || item.contains?.(event.target));
      if (panel) {
        lastFocusedPanel = panel.dataset.panel;
        lastFocusedElement = event.target;
      }
    });
  }
  if (doc.defaultView?.addEventListener) {
    doc.defaultView.addEventListener("resize", () => {
      const before = settings.panel;
      update({ retainFocusedPanel: true, restoreBlurredFocus: true });
      if (before !== settings.panel) savePreferences(storage, settings);
    });
  }
  update();
  return { get preferences() { return { ...settings }; }, change,
    get libraryElement() { return lib; } };
}

if (typeof document !== "undefined") {
  let storage;
  try { storage = typeof window !== "undefined" ? window.localStorage : undefined; }
  catch (_) { storage = undefined; }
  initViewModes(document, storage);
}
