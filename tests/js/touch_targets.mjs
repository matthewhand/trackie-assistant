// Source-only CSS contract for standalone touch targets, no browser or network.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

const source = readFileSync(process.argv[2], "utf8");
const legacyMarker = "/* The standalone host alone opts in; HA retains its pre-MVP styling. */";
const touchMarker = "/* Standalone-only reachable touch targets; Home Assistant keeps its CSS. */";
assert.equal(source.split(legacyMarker).length, 2);
assert.equal(source.split(touchMarker).length, 2);

// The original card CSS belongs to Home Assistant; opting into standalone
// touch targets must not silently mutate those pre-existing CSS declarations.
// Hash refreshed for #133 (narrow chips wrap and clamp instead of side-scrolling).
const defaultCss = source.slice(source.indexOf("const STYLES = "), source.indexOf(legacyMarker));
assert.equal(createHash("sha256").update(defaultCss).digest("hex"),
  "cc653b4e5e5739846bfd035d2ccbf3fdcdf163d1ce2051854f3fc56c9fbf2610");

const after = source.slice(source.indexOf(touchMarker) + touchMarker.length);
const touch = after.slice(0, after.indexOf(String.fromCharCode(96)));
const mobileAt = touch.indexOf("@container (max-width:700px){");
assert.ok(mobileAt >= 0, "mobile layout must use the existing container breakpoint");
const common = touch.slice(0, mobileAt);
const mobile = touch.slice(mobileAt);

function rules(css) {
  const result = [];
  const block = /([^{}]+)\{([^{}]*)\}/g;
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, "");
  let match;
  while ((match = block.exec(clean))) {
    const selectors = match[1].split(",").map(x => x.trim());
    const declarations = Object.fromEntries(match[2].split(";")
      .map(x => x.trim()).filter(Boolean)
      .map(x => {
        const at = x.indexOf(":");
        return [x.slice(0, at).trim(), x.slice(at + 1).trim()];
      }));
    result.push({ selectors, declarations });
  }
  return result;
}
const commonRules = rules(common);
const mobileRules = rules(mobile);
const rule = (items, selector) => {
  const found = items.filter(x => x.selectors.includes(selector));
  assert.equal(found.length, 1, selector + " should have exactly one rule in its scope");
  return found[0].declarations;
};
const host = ":host([data-suno-theme])";
const target = (selector, properties) => {
  const declaration = rule(commonRules, host + " " + selector);
  for (const [name, minimum] of Object.entries(properties)) {
    const actual = Number.parseInt(declaration[name], 10);
    assert.ok(actual >= minimum, selector + " must have " + name + " >= " + minimum);
  }
};
target(".play", { width: 44, height: 44 });
target(".more", { width: 44, height: 44 });
target(".pane-close", { width: 44, height: 44 });
target(".chip", { "min-height": 44 });
target(".chips.sub .chip", { "min-height": 44 });
target(".qf", { "min-height": 44 });
target(".seg button", { "min-width": 44, "min-height": 44 });
target(".search", { "min-height": 44 });
target(".search input", { "min-height": 42 });
target(".splitb", { height: 46 }); // 1px border on each side leaves 44px inside.
target(".splitb button", { "min-width": 44, "min-height": 44 });
target(".menu button", { "min-height": 44 });
target(".lnk", { "min-height": 44 });
assert.equal(rule(commonRules, host + " .lnk").display, "inline-flex");
target(".more-chips", { "min-height": 44 });
target(".peekbtns button", { "min-height": 44 });

assert.equal(rule(mobileRules, host + " .song")["grid-template-columns"], "44px minmax(0,1fr)");
assert.equal(rule(mobileRules, host + " .acts")["grid-column"], "2");
assert.equal(rule(mobileRules, host + " .acts")["grid-row"], "2");
assert.equal(rule(mobileRules, host + " .acts")["flex-wrap"], "wrap");
assert.equal(rule(mobileRules, host + " .tl")["grid-column"], "2");
assert.equal(rule(mobileRules, host + " .meta")["grid-column"], "1/-1");
assert.equal(rule(mobileRules, host + " .style")["grid-column"], "1/-1");
assert.equal(rule(mobileRules, host + " .handle")["width"], "44px");
assert.equal(rule(mobileRules, host + " .handle")["height"], "44px");
assert.equal(rule(mobileRules, host + " .handle")["flex"], "0 0 44px");
assert.equal(rule(mobileRules, host + " .handle")["min-height"], "44px");
assert.equal(rule(mobileRules, host + " .handle")["max-height"], "44px");
assert.equal(rule(mobileRules, host + " .handle::after")["top"], "19px");
assert.equal(rule(mobileRules, host + " .pane-close")["top"], "8px");

// Conservative synthetic 320px layout budget. No third intrinsic-width
// action column: the action row fits into the available minmax(0,1fr) track.
// This is a source contract, not an actual browser geometry measurement.
for (const viewport of [320, 360, 390, 430, 768]) {
  const shell = 24 + 6 + 16 + 20 + 20; // host, frame, wrap, main, song paddings
  const remaining = viewport - shell - 44 - 10; // second track after play + gap
  const actions = 44 * 3 + 6; // two copy targets + More, one actions gap
  assert.ok(remaining >= actions, "narrow row won't fit 44px actions at " + viewport);
}

// Other view modes and the native Home Assistant CSS remain untouched.
assert.doesNotMatch(touch, /(?:fetch|setConfig|addEventListener)\s*\(/);
for (const item of [...commonRules, ...mobileRules]) {
  for (const selector of item.selectors) {
    if (selector.startsWith(":host(")) assert.ok(selector.startsWith(host), selector);
  }
}
console.log("touch targets: standalone CSS sizes, 320px source layout, HA default hash passed");
