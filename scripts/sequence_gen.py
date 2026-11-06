"""Sequence-diagram generator for suno-download-cli documentation.

Separate from diagram_gen.py because sequence diagrams have a different layout
grammar (lifelines, activation bars, message arrows, combined fragments) that
shares nothing with node/edge boxes.

Usage: python scripts/sequence_gen.py <spec.json> [--out docs/diagrams/x.html]

Spec shape::

    {
      "slug": "seq-happy",
      "eyebrow": "Sequence · Happy path",
      "title": "...", "desc": "...",
      "viewbox": [1000, 540], "width": 1120,
      "actors": [{"id":"user","name":"User","sub":"shell"}, ...],
      "messages": [
        {"n":1,"from":"user","to":"cli","text":"suno-dl --url ...","kind":"call",
         "activation":{"who":"cli","start":1,"end":8}},
        ...
      ],
      "fragment": {"op":"alt","y":300,"h":160,"guard":"[media 403]",
                   "divider":380,"alt_guard":"[else]"}
    }

Message kinds: call (solid), return (dashed + filled), async (dashed + open),
headline (solid accent). Time flows strictly top to bottom; arrows never rise.
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
LINK = "#2e5aa8"
LIFELINE = "rgba(45,49,66,0.22)"
ACT_FILL = "rgba(45,49,66,0.06)"

KIND_STYLE = {
    "call": (MUTED, "arrow", ""),
    "call-link": (LINK, "arrow-link", ""),
    "return": (MUTED, "arrow", ' stroke-dasharray="5,4"'),
    "async": (MUTED, "arrow-open", ' stroke-dasharray="5,4"'),
    "headline": (ACCENT, "arrow-accent", ""),
}


def _esc(text: str) -> str:
    """Escape XML-significant characters in text nodes."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class Actor:
    id: str
    name: str
    sub: str
    cx: int


class Sequence:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        n = len(spec["actors"])
        vb_w = spec["viewbox"][0]
        margin = 96
        span = vb_w - 2 * margin
        self.actors: dict[str, Actor] = {}
        for i, a in enumerate(spec["actors"]):
            cx = margin + (span * i) // max(n - 1, 1)
            self.actors[a["id"]] = Actor(a["id"], a["name"], a.get("sub", ""), cx)

    def _msg_svg(self, m: dict) -> tuple[str, str]:
        src = self.actors[m["from"]]
        dst = self.actors[m["to"]]
        y = m["y"]
        stroke, marker, dash = KIND_STYLE.get(m.get("kind", "call"), KIND_STYLE["call"])
        if src.cx == dst.cx:
            # self-message: U-loop to the right
            x = src.cx
            d = f"M {x},{y} H {x + 40} V {y + 28} H {x + 4}"
            return (
                f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.2"'
                f'{dash} marker-end="url(#{marker})"/>',
                "",
            )
        # horizontal; arrowhead points right or left
        x1, x2 = src.cx, dst.cx
        path = (
            f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{stroke}" '
            f'stroke-width="1.2"{dash} marker-end="url(#{marker})"/>'
        )
        label = m.get("text", "")
        if not label:
            return path, ""
        w = max(len(label) * 5 + 12, 40)
        lx = (x1 + x2) // 2
        lay = (
            f'<rect x="{lx - w // 2}" y="{y - 18}" width="{w}" height="12" rx="2" fill="{PAPER}"/>'
            f'<text x="{lx}" y="{y - 9}" fill="#7a8399" font-size="8" '
            f'font-family="\'Geist Mono\', monospace" text-anchor="middle" '
            f'letter-spacing="0.04em">{label}</text>'
        )
        return path, lay

    def render_svg(self) -> str:
        s = self.spec
        slug = s["slug"]
        vb_w, vb_h = s["viewbox"]
        top = 116
        bottom = vb_h - 40
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
            '    <marker id="arrow-link" markerWidth="8" markerHeight="6" refX="7" refY="3" '
            f'orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{LINK}"/></marker>',
            '    <marker id="arrow-open" markerWidth="8" markerHeight="6" refX="7" refY="3" '
            f'orient="auto"><polyline points="0 0, 8 3, 0 6" fill="none" stroke="{MUTED}" '
            'stroke-width="1.2"/></marker>',
            "  </defs>",
            f'  <rect width="100%" height="100%" fill="{PAPER}"/>',
        ]

        # lifelines
        for a in self.actors.values():
            out.append(
                f'  <line x1="{a.cx}" y1="{top}" x2="{a.cx}" y2="{bottom}" '
                f'stroke="{LIFELINE}" stroke-width="1" stroke-dasharray="3,3"/>'
            )

        # combined fragment frame (optional)
        frag = s.get("fragment")
        if frag:
            x0 = (
                min(
                    self.actors[m["from"]].cx
                    for m in s["messages"]
                    if frag["y"] <= m["y"] <= frag["y"] + frag["h"]
                )
                - 32
            )
            x1 = (
                max(
                    self.actors[m["to"]].cx
                    for m in s["messages"]
                    if frag["y"] <= m["y"] <= frag["y"] + frag["h"]
                )
                + 32
            )
            out.append(
                f'  <rect x="{x0}" y="{frag["y"]}" width="{x1 - x0}" height="{frag["h"]}" rx="4" '
                'fill="rgba(45,49,66,0.02)" stroke="rgba(45,49,66,0.22)" stroke-width="1"/>'
            )
            out.append(
                f'  <rect x="{x0}" y="{frag["y"]}" width="44" height="16" rx="2" fill="{PAPER}" '
                'stroke="rgba(45,49,66,0.22)" stroke-width="1"/>'
            )
            out.append(
                f'  <text x="{x0 + 22}" y="{frag["y"] + 12}" fill="{MUTED}" font-size="8" '
                f'font-family="\'Geist Mono\', monospace" text-anchor="middle" '
                f'letter-spacing="0.12em">{frag["op"].upper()}</text>'
            )
            if frag.get("guard"):
                out.append(
                    f'  <text x="{x0 + 52}" y="{frag["y"] + 12}" fill="{MUTED}" font-size="8" '
                    f"font-family=\"'Geist Mono', monospace\" "
                    f'letter-spacing="0.04em">{frag["guard"]}</text>'
                )
            if frag.get("divider"):
                dy = frag["divider"]
                out.append(
                    f'  <line x1="{x0 + 8}" y1="{dy}" x2="{x1 - 8}" y2="{dy}" '
                    'stroke="rgba(45,49,66,0.20)" stroke-width="1" stroke-dasharray="4,3"/>'
                )
            if frag.get("alt_guard"):
                gy = frag["divider"] + 14
                out.append(
                    f'  <text x="{x0 + 12}" y="{gy}" fill="{MUTED}" font-size="8" '
                    f"font-family=\"'Geist Mono', monospace\" "
                    f'letter-spacing="0.04em">{frag["alt_guard"]}</text>'
                )

        # activation bars (before arrows)
        for act in s.get("activations", []):
            a = self.actors[act["who"]]
            out.append(
                f'  <rect x="{a.cx - 4}" y="{act["y"]}" width="8" height="{act["h"]}" '
                f'fill="{ACT_FILL}" stroke="{MUTED}" stroke-width="0.8"/>'
            )

        # messages: paths first, then labels (so labels sit above strokes)
        paths, labels = [], []
        for m in sorted(s["messages"], key=lambda m: m["y"]):
            p, lbl = self._msg_svg(m)
            paths.append("  " + p)
            if lbl:
                labels.append("  " + lbl)
        out += paths + labels

        # actor boxes (drawn last so lifelines don't cross them)
        for a in self.actors.values():
            out.append(
                f'  <rect x="{a.cx - 72}" y="56" width="144" height="52" rx="6" fill="{PAPER}"/>'
            )
            out.append(
                f'  <rect x="{a.cx - 72}" y="56" width="144" height="52" rx="6" fill="#ffffff" '
                f'stroke="{INK}" stroke-width="1"/>'
            )
            out.append(
                f'  <text x="{a.cx}" y="{a.name and 82 or 82}" fill="{INK}" font-size="12" '
                f'font-weight="600" font-family="\'Geist\', sans-serif" '
                f'text-anchor="middle">{_esc(a.name)}</text>'
            )
            if a.sub:
                out.append(
                    f'  <text x="{a.cx}" y="{98}" fill="{MUTED}" font-size="9" '
                    f"font-family=\"'Geist Mono', monospace\" "
                    f'text-anchor="middle">{_esc(a.sub)}</text>'
                )

        # legend
        ly = vb_h - 22
        out.append(
            f'  <line x1="32" y1="{ly - 8}" x2="{vb_w - 32}" y2="{ly - 8}" '
            'stroke="rgba(45,49,66,0.10)" stroke-width="0.8"/>'
        )
        items = [
            ("CALL", MUTED, ""),
            ("RETURN", MUTED, ' stroke-dasharray="5,4"'),
            ("HEADLINE", ACCENT, ""),
        ]
        lx = 32
        out.append(
            f'  <text x="{lx}" y="{ly + 8}" fill="{MUTED}" font-size="8" '
            f'font-family="\'Geist Mono\', monospace" letter-spacing="0.14em">LEGEND</text>'
        )
        lx = 112
        for txt, col, dash in items:
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
        print("usage: sequence_gen.py <spec.json> [--out FILE]")
        return 2
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text())
    out = (
        Path(sys.argv[sys.argv.index("--out") + 1])
        if "--out" in sys.argv
        else spec_path.with_suffix(".html")
    )
    out.write_text(Sequence(spec).render_html())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
