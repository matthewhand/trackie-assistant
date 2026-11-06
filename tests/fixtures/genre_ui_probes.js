// Browser-side probes for tests/test_genre_ui_viewports.py. Installed into every page as
// window.__probe; each probe reads *computed* layout / style / ARIA state from the real card.
(() => {
  const root = () => document.getElementById("lib").shadowRoot;
  const rect = (el) => el.getBoundingClientRect();

  const probe = {
    root,

    /** No page-level horizontal scroll and nothing outside the viewport (chip strips scroll by design). */
    noOverflow() {
      if (document.documentElement.scrollWidth > innerWidth + 1) return false;
      for (const el of root().querySelectorAll(".wrap *")) {
        if (el.closest(".chips")) continue;
        const r = rect(el);
        if (r.width && (r.right > innerWidth + 1 || r.left < -1)) return false;
      }
      return true;
    },

    geometry() {
      const g = (s) => {
        const b = rect(root().querySelector(s));
        return { l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width };
      };
      return { pane: g("#genre-pane"), main: g(".main"), vw: innerWidth, vh: innerHeight };
    },

    /** Inside the viewport and exactly what a tap at the element's centre would hit. */
    hit(sel) {
      const el = root().querySelector(sel);
      if (!el) return false;
      const r = rect(el);
      if (!r.width || !r.height) return false;
      if (r.left < -1 || r.top < -1 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1) return false;
      const top = root().elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !!top && (top === el || el.contains(top));
    },

    smallTargets(min) {
      const out = [];
      for (const el of root().querySelectorAll("button, a[href], textarea, [role=button], input")) {
        const cs = getComputedStyle(el);
        if (cs.visibility === "hidden" || cs.display === "none") continue;
        if (min < 40 && el.matches("a.title")) continue; // inline text link (WCAG 2.5.8 exception)
        const t = el.tagName === "INPUT" ? el.closest("label") || el : el;
        const b = rect(t);
        if (!b.width || !b.height) continue;
        if (b.width < min || b.height < min)
          out.push((el.id || el.className || el.tagName) + " " + Math.round(b.width) + "x" + Math.round(b.height));
      }
      return out;
    },

    chipProblems() {
      const out = [];
      for (const row of root().querySelectorAll(".chips")) {
        const chips = Array.from(row.querySelectorAll(".chip"));
        chips.forEach((c, i) => {
          if (c.scrollWidth > c.clientWidth + 1) out.push("clipped: " + c.textContent.trim());
          const next = chips[i + 1];
          if (next && Math.abs(rect(c).top - rect(next).top) < 4 && rect(c).right > rect(next).left + 0.5)
            out.push("overlap: " + c.textContent.trim() + " / " + next.textContent.trim());
        });
      }
      for (const s of root().querySelectorAll(".song .lnk, .seg button, .qf"))
        if (s.getClientRects()[0].height > 60) out.push("wrapped: " + s.textContent.trim());
      return out;
    },

    songRowProblems() {
      const out = [];
      const hit = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
      for (const song of root().querySelectorAll(".song")) {
        const t = rect(song.querySelector("a.title"));
        if (t.width < 60) out.push("title collapsed to " + Math.round(t.width) + "px");
        const acts = Array.from(song.querySelectorAll(".acts button")).map(rect);
        const others = Array.from(song.querySelectorAll(".tl > *, .meta > *"))
          .filter((e) => e.getClientRects().length)
          .map((e) => [e.className || e.tagName, rect(e)]);
        for (const a of acts) for (const [n, r] of others) if (hit(a, r)) out.push("controls overlap " + n);
      }
      return out;
    },

    titles() {
      return Array.from(root().querySelectorAll(".song")).map((s) => {
        const a = s.querySelector("a.title");
        const cs = getComputedStyle(a);
        return {
          id: a.getAttribute("href").split("/").pop(),
          text: a.textContent,
          tip: a.getAttribute("title"),
          ws: cs.whiteSpace,
          clipped: a.scrollWidth > a.clientWidth + 1 || a.scrollHeight > a.clientHeight + 1,
          kids: a.children.length,
        };
      });
    },

    unnamedOrUnfocusable() {
      const out = [];
      for (const el of root().querySelectorAll("button, a[href], input, textarea")) {
        const cs = getComputedStyle(el);
        if (cs.display === "none" || cs.visibility === "hidden") continue;
        if (el.tabIndex < 0) out.push("not focusable: " + (el.id || el.className));
        const lab = el.closest("label");
        const name = (el.getAttribute("aria-label") || el.textContent || el.getAttribute("title") || "").trim();
        if (!name && !(lab && lab.textContent.trim()) && !el.getAttribute("aria-label"))
          out.push("no accessible name: " + (el.id || el.className));
      }
      return out;
    },

    activeInPane() {
      const a = root().activeElement;
      return !!a && !!a.closest("#genre-pane");
    },
    activeAttr(name) {
      const a = root().activeElement;
      return a ? a.getAttribute(name) : null;
    },
    click(sel) {
      root().querySelector(sel).click();
    },
    watchAlert() {
      window.__alertMuts = 0;
      new MutationObserver((m) => {
        window.__alertMuts += m.length;
      }).observe(root().querySelector("#sg-alert"), { childList: true, characterData: true, subtree: true });
    },
    keepLiveNode() {
      window.__liveNode = root().querySelector("#sg-live");
    },
    sameLiveNode() {
      return window.__liveNode === root().querySelector("#sg-live");
    },

    /** WCAG AA contrast of every visible text node against its effective background. */
    contrast() {
      const parse = (s) => {
        const m = s.match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
      };
      const over = (f, b) => ({ r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a), a: 1 });
      const lum = (c) => {
        const f = (v) => ((v /= 255) <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
        return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
      };
      const up = (e) => e.parentElement || (e.getRootNode && e.getRootNode().host);
      const effBg = (el) => {
        const chain = [];
        for (let e = el; e; e = up(e)) chain.push(e);
        let bg = { r: 255, g: 255, b: 255, a: 1 };
        for (const e of chain.reverse()) {
          const c = parse(getComputedStyle(e).backgroundColor);
          if (c && c.a > 0) bg = over(c, bg);
        }
        return bg;
      };
      const opac = (el) => {
        let o = 1;
        for (let e = el; e && e.nodeType === 1; e = up(e)) o *= parseFloat(getComputedStyle(e).opacity);
        return o;
      };
      const out = [];
      const w = document.createTreeWalker(root(), NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        if (!n.textContent.trim()) continue;
        const el = n.parentElement;
        if (!el || el.closest("style") || el.closest(".sr-only")) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === "hidden" || cs.display === "none") continue;
        const r = rect(el);
        if (!r.width || !r.height || el.closest("button:disabled")) continue;
        const bg = effBg(el);
        const fg0 = parse(cs.color);
        if (!fg0) continue;
        const f = over({ ...fg0, a: fg0.a * opac(el) }, bg);
        const [a, b] = [lum(f), lum(bg)];
        const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        const size = parseFloat(cs.fontSize);
        const need = size >= 24 || (size >= 18.66 && parseInt(cs.fontWeight) >= 700) ? 3 : 4.5;
        if (ratio < need)
          out.push({ sel: (el.id || el.className || el.tagName) + ":" + n.textContent.trim().slice(0, 18), ratio: Math.round(ratio * 100) / 100, need });
      }
      return out;
    },
  };
  window.__probe = probe;
})();
