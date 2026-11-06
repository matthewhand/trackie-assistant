"""State-machine diagram generator for suno-download-cli documentation.

Separate from diagram_gen.py because a state machine has its own grammar:
rounded state boxes, a filled start dot, a ringed end state, and labelled
transitions with guards.

Usage: python scripts/state_gen.py <spec.json> [--out docs/diagrams/x.html]

Spec shape::

    {
      "slug": "state", "eyebrow": "...", "title": "...", "desc": "...",
      "viewbox": [1000, 420], "width": 1120,
      "states": [{"id":"start","kind":"start","x":80,"y":200},
                 {"id":"dl","name":"downloaded","x":300,"y":180,"w":200,"h":56},
                 {"id":"end","kind":"end","x":860,"y":200}],
      "transitions": [{"from":"start","to":"dl","label":"try candidate","kind":"local",
                       "guard":"[size >= 1 KiB]"}]
    }

Transition kinds: local (muted), accent (coral), async (dashed).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

PAPER = "#f5f5f5"
INK = "#2d3142"
MUTED = "#4f5d75"
ACCENT = "#eb6c36"

KIND_STYLE = {
    "local": (MUTED, "arrow", ""),
    "accent": (ACCENT, "arrow-accent", ""),
    "async": (MUTED, "arrow", ' stroke-dasharray="4,3"'),
}


def _esc(text: str) -> str:
    """Escape XML-significant characters in text nodes."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class State:
    id: str
    kind: str
    x: int
    y: int
    w: int
    h: int
    name: str = ""
    sub: str = ""

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


class StateMachine:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.states: dict[str, State] = {}
        for s in spec["states"]:
            self.states[s["id"]] = State(
                id=s["id"],
                kind=s.get("kind", "state"),
                x=s.get("x", 0),
                y=s.get("y", 0),
                w=s.get("w", 200),
                h=s.get("h", 56),
                name=s.get("name", ""),
                sub=s.get("sub", ""),
            )
        # distinct horizontal lane per (source, target) so parallel transitions
        # never share a stroke path or stack their labels
        self._lane: dict[str, int] = {}

    def _lane_for(self, key: str) -> int:
        n = self._lane.get(key, 0)
        self._lane[key] = n + 1
        return n * 20

    def _edge(self, t: dict) -> tuple[str, str]:
        a, b = self.states[t["from"]], self.states[t["to"]]
        stroke, marker, dash = KIND_STYLE.get(t.get("kind", "local"), KIND_STYLE["local"])
        sx, sy = a.right, a.cy
        ex, ey = b.x, b.cy
        lane = self._lane_for(f"{t['from']}->{t['to']}")
        mid = (sx + ex) // 2 + lane
        r = 8
        d = (
            f"M {sx},{sy} H {mid - r} Q {mid},{sy} {mid},{sy + (r if ey > sy else -r)} "
            f"V {ey - (r if ey > sy else -r)} Q {mid},{ey} {mid + r},{ey} H {ex}"
        )
        path = (
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.2"'
            f'{dash} marker-end="url(#{marker})"/>'
        )
        label = t.get("label", "")
        guard = t.get("guard", "")
        out = ""
        # Put the label on the vertical segment (open canvas) rather than over
        # the horizontal exit, which is where siblings collide.
        vmid_y = (sy + ey) // 2
        if label:
            w = max(len(label) * 6 + 12, 44)
            out += (
                f'<rect x="{mid - w // 2}" y="{vmid_y - 14}" width="{w}" height="12" rx="2" '
                f'fill="{PAPER}"/>'
                f'<text x="{mid}" y="{vmid_y - 5}" fill="#7a8399" font-size="8" '
                f'font-family="\'Geist Mono\', monospace" text-anchor="middle" '
                f'letter-spacing="0.04em">{_esc(label)}</text>'
            )
        if guard:
            gw = max(len(guard) * 6 + 12, 44)
            out += (
                f'<rect x="{mid - gw // 2}" y="{vmid_y}" width="{gw}" height="12" rx="2" '
                f'fill="{PAPER}"/>'
                f'<text x="{mid}" y="{vmid_y + 9}" fill="#9aa3b2" font-size="7" '
                f'font-family="\'Geist Mono\', monospace" text-anchor="middle" '
                f'letter-spacing="0.04em">{_esc(guard)}</text>'
            )
        return path, out

    def render_svg(self) -> str:
        s = self.spec
        slug = s["slug"]
        vb_w, vb_h = s["viewbox"]
        out: list[str] = [
            f'<svg viewBox="0 0 {vb_w} {vb_h}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img" aria-labelledby="{slug}-title {slug}-desc">',
            f'  <title id="{slug}-title">{s["title"]}</title>',
            f'  <desc id="{slug}-desc">{s["desc"]}</desc>',
            "  <defs>",
            '    <marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" '
            f'orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{MUTED}"/></marker>',
            '    <marker id="arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" '
            f'orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{ACCENT}"/></marker>',
            "  </defs>",
            f'  <rect width="100%" height="100%" fill="{PAPER}"/>',
        ]

        paths, labels = [], []
        for t in s["transitions"]:
            p, lbl = self._edge(t)
            paths.append("  " + p)
            if lbl:
                labels.append("  " + lbl)
        out += paths + labels

        for st in self.states.values():
            if st.kind == "start":
                out.append(f'  <circle cx="{st.cx}" cy="{st.cy}" r="10" fill="{INK}"/>')
            elif st.kind == "end":
                out.append(
                    f'  <circle cx="{st.cx}" cy="{st.cy}" r="12" fill="none" '
                    f'stroke="{INK}" stroke-width="1.4"/>'
                )
                out.append(f'  <circle cx="{st.cx}" cy="{st.cy}" r="7" fill="{INK}"/>')
            else:
                fill = "rgba(235,108,54,0.08)" if st.kind == "focal" else "#ffffff"
                stroke = ACCENT if st.kind == "focal" else INK
                out.append(
                    f'  <rect x="{st.x}" y="{st.y}" width="{st.w}" height="{st.h}" '
                    f'rx="6" fill="{PAPER}"/>'
                )
                out.append(
                    f'  <rect x="{st.x}" y="{st.y}" width="{st.w}" height="{st.h}" rx="6" '
                    f'fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
                )
                ny = st.cy + 3 if not st.sub else st.cy - 3
                out.append(
                    f'  <text x="{st.cx}" y="{ny}" fill="{INK}" font-size="12" '
                    f'font-weight="600" font-family="\'Geist\', sans-serif" '
                    f'text-anchor="middle">{_esc(st.name)}</text>'
                )
                if st.sub:
                    out.append(
                        f'  <text x="{st.cx}" y="{st.cy + 13}" fill="{MUTED}" font-size="9" '
                        f"font-family=\"'Geist Mono', monospace\" "
                        f'text-anchor="middle">{_esc(st.sub)}</text>'
                    )

        # legend
        ly = vb_h - 22
        out.append(
            f'  <line x1="32" y1="{ly - 8}" x2="{vb_w - 32}" y2="{ly - 8}" '
            'stroke="rgba(45,49,66,0.10)" stroke-width="0.8"/>'
        )
        out.append(
            f'  <text x="32" y="{ly + 8}" fill="{MUTED}" font-size="8" '
            f'font-family="\'Geist Mono\', monospace" letter-spacing="0.14em">LEGEND</text>'
        )
        lx = 112
        for txt, col, dash in (
            ("TRANSITION", MUTED, ""),
            ("SUCCESS", ACCENT, ""),
            ("DISCARD", MUTED, ' stroke-dasharray="4,3"'),
        ):
            out.append(
                f'  <line x1="{lx}" y1="{ly + 4}" x2="{lx + 24}" y2="{ly + 4}" '
                f'stroke="{col}" stroke-width="1.2"{dash}/>'
            )
            lx += 32
            out.append(
                f'  <text x="{lx}" y="{ly + 8}" fill="{MUTED}" font-size="8" '
                f"font-family=\"'Geist Mono', monospace\">{txt}</text>"
            )
            lx += len(txt) * 6 + 32
        out.append("</svg>")
        return "\n    ".join(out)

    def render_html(self) -> str:
        s = self.spec
        fonts = (
            "https://fonts.googleapis.com/css2?"
            "family=Instrument+Serif:ital@0;1"
            "&family=Geist:wght@400;500;600"
            "&family=Geist+Mono:wght@400;500;600"
            "&display=swap"
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{s["title"]}</title>
  <link href="{fonts}" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --color-paper: {PAPER}; --color-ink: {INK}; --color-muted: {MUTED};
      --font-sans: 'Geist', system-ui, sans-serif;
      --font-serif: 'Instrument Serif', serif;
      --font-mono: 'Geist Mono', ui-monospace, monospace;
    }}
    body {{ font-family: var(--font-sans); background: var(--color-paper); color: var(--color-ink);
            min-height: 100vh; display: flex; align-items: center; justify-content: center;
            padding: 3rem 2rem; }}
    .frame {{ max-width: {s.get("width", 1120)}px; width: 100%; }}
    .eyebrow {{ font-family: var(--font-mono); font-size: 0.66rem; font-weight: 500;
                letter-spacing: 0.18em; text-transform: uppercase; color: var(--color-muted);
                margin-bottom: 0.5rem; }}
    h1 {{ font-family: var(--font-serif); font-size: clamp(1.5rem, 2.4vw + 0.75rem, 2rem);
          font-weight: 400; letter-spacing: -0.02em; line-height: 1.15; margin-bottom: 1.5rem; }}
    svg {{ width: 100%; min-width: 900px; display: block; }}
  </style>
</head>
<body>
  <div class="frame">
    <p class="eyebrow">{s["eyebrow"]}</p>
    <h1>{s["title"]}</h1>
    {self.render_svg()}
  </div>
</body>
</html>
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: state_gen.py <spec.json> [--out FILE]")
        return 2
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text())
    out = (
        Path(sys.argv[sys.argv.index("--out") + 1])
        if "--out" in sys.argv
        else spec_path.with_suffix(".html")
    )
    out.write_text(StateMachine(spec).render_html())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
