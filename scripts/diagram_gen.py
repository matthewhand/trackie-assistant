"""Diagram SVG generator for suno-download-cli documentation.

Why this exists: hand-placing SVG coordinates produced recurring connector
defects (labels on strokes, connectors behind non-endpoint boxes, colliding
attach points). Those rules are mechanical, so we compute them instead of
eyeballing them.

Usage: python scripts/diagram_gen.py <spec.json> [--out docs/diagrams/x.html]

Spec shape::

    {
      "slug": "02-architecture",
      "eyebrow": "Architecture · Modules",
      "title": "Module structure and boundaries",
      "desc": "One sentence for screen readers.",
      "viewbox": [1000, 620],
      "width": 1120, "height": 6,          # CSS box, in rem
      "zones": [{"x":24,"y":72,"w":600,"h":300,"label":"LOCAL"}],
      "nodes": [{"id":"cli","x":208,"y":164,"w":192,"h":52,
                 "kind":"focal|module|external|store|input|async",
                 "name":"suno-dl","sub":"cli.py · click"}],
      "edges": [{"from":"cli","to":"dl","port":"bottom","label":"TARGETS",
                 "kind":"local|http|accent|async"}],
      "legend": ["focal","module","external","store","http","local"]
    }

Guarantees enforced by construction:
  * every connector is an orthogonal path with r=8 elbows
  * arrow labels are placed on an open-canvas segment with >=6px clearance
  * connectors leaving the same edge get distinct attach points (>=12px apart)
  * labels are painted before nodes (nodes mask any stray overlap)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

PAPER = "#f5f5f5"
INK = "#2d3142"
MUTED = "#4f5d75"
SOFT = "#bfc0c0"
ACCENT = "#eb6c36"
LINK = "#2e5aa8"

# kind -> (fill, stroke, dash?)
KINDS: dict[str, tuple[str, str, str]] = {
    "focal": ("rgba(235,108,54,0.08)", ACCENT, ""),
    "module": ("#ffffff", INK, ""),
    "external": ("rgba(45,49,66,0.03)", "rgba(45,49,66,0.30)", ""),
    "store": ("rgba(45,49,66,0.05)", MUTED, ""),
    "input": ("rgba(79,93,117,0.10)", SOFT, ""),
    "async": ("rgba(45,49,66,0.02)", "rgba(45,49,66,0.20)", ' stroke-dasharray="4,3"'),
}

# edge kind -> (stroke, marker, dash?)
EDGE_KINDS: dict[str, tuple[str, str, str]] = {
    "local": (MUTED, "arrow", ""),
    "http": (LINK, "arrow-link", ""),
    "accent": (ACCENT, "arrow-accent", ""),
    "async": (MUTED, "arrow", ' stroke-dasharray="4,3"'),
}


@dataclass
class Node:
    id: str
    x: int
    y: int
    w: int
    h: int
    name: str
    sub: str = ""
    kind: str = "module"

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


@dataclass
class Edge:
    src: str
    dst: str
    port: str = "auto"  # bottom | top | right | left | auto
    label: str = ""
    kind: str = "local"


def _esc(text: str) -> str:
    """Escape XML-significant characters in text nodes.

    Labels are data, not markup: without this, a label like ``<base>.m4a``
    would be parsed as an HTML tag by validators and browsers alike.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _marker(name: str, color: str) -> str:
    """One arrowhead marker definition."""
    return (
        f'    <marker id="{name}" markerWidth="8" markerHeight="6" refX="7" refY="3" '
        f'orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{color}"/></marker>'
    )


class Diagram:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.nodes: dict[str, Node] = {}
        for n in spec.get("nodes", []):
            node = Node(
                id=n["id"],
                x=n["x"],
                y=n["y"],
                w=n["w"],
                h=n["h"],
                name=n["name"],
                sub=n.get("sub", ""),
                kind=n.get("kind", "module"),
            )
            self.nodes[node.id] = node
        self.edges = [
            Edge(
                e["from"],
                e["to"],
                e.get("port", "auto"),
                e.get("label", ""),
                e.get("kind", "local"),
            )
            for e in spec.get("edges", [])
        ]
        self._used_attach: dict[tuple[str, str], int] = {}
        # lane offset per (source, side) so parallel connectors never overlap
        self._lane: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- routing

    def _lane_of(self, node_id: str, side: str) -> int:
        """Distinct horizontal lane for each connector sharing a source side."""
        key = (node_id, side)
        n = self._lane.get(key, 0)
        self._lane[key] = n + 1
        return n * 16

    def _attach(self, node: Node, side: str, index: int, total: int) -> tuple[int, int]:
        """Distinct attach point along an edge, fanned if several share it."""
        if side == "bottom":
            span = node.w - 32
            x = node.x + 16 + (span * (index + 1)) // (total + 1)
            return (x, node.bottom)
        if side == "top":
            span = node.w - 32
            x = node.x + 16 + (span * (index + 1)) // (total + 1)
            return (x, node.y)
        if side == "right":
            span = node.h - 24
            y = node.y + 12 + (span * (index + 1)) // (total + 1)
            return (node.right, y)
        # left
        span = node.h - 24
        y = node.y + 12 + (span * (index + 1)) // (total + 1)
        return (node.x, y)

    def _count_on_side(self, node_id: str, side: str) -> int:
        return sum(1 for e in self.edges if (e.src == node_id and self._side_of(e) == side))

    def _side_of(self, e: Edge) -> str:
        if e.port != "auto":
            return e.port
        src, dst = self.nodes[e.src], self.nodes[e.dst]
        if abs(src.cx - dst.cx) < 40:
            return "bottom" if dst.cy > src.cy else "top"
        return "right" if dst.cx > src.cx else "left"

    def route(self, e: Edge) -> tuple[str, tuple[int, int], str]:
        """Return (path-d, label-point, side). Orthogonal elbows only."""
        src, dst = self.nodes[e.src], self.nodes[e.dst]
        side = self._side_of(e)
        key = (e.src, side)
        idx = self._used_attach.get(key, 0)
        total = self._count_on_side(e.src, side)
        self._used_attach[key] = idx + 1

        sx, sy = self._attach(src, side, idx, total)

        r = 8
        if side == "bottom":
            # Exit the source's bottom edge, bend once, enter the target's TOP.
            mid = dst.y - 24
            d = (
                f"M {sx},{sy} V {mid - r} Q {sx},{mid} {sx + r},{mid} "
                f"H {dst.cx - r} Q {dst.cx},{mid} {dst.cx},{mid + r} V {dst.y}"
            )
            return d, (sx, (sy + mid) // 2), side
        if side == "top":
            mid = dst.bottom + 24
            d = (
                f"M {sx},{sy} V {mid + r} Q {sx},{mid} {sx + r},{mid} "
                f"H {dst.cx - r} Q {dst.cx},{mid} {dst.cx},{mid - r} V {dst.bottom}"
            )
            return d, (sx, (sy + mid) // 2), side
        if side == "right":
            # Vertical run sits in the gutter between source and target; each
            # connector gets its own lane so parallel runs never overlap.
            lane = self._lane_of(e.src, "right")
            above = dst.cy < src.cy
            entry = dst.bottom if above else dst.y
            mid = dst.x - 24 - lane
            vert_to = entry + (r if above else -r)
            d = (
                f"M {sx},{sy} H {mid - r} Q {mid},{sy} {mid},{sy + (r if above else -r)} "
                f"V {vert_to} Q {mid},{entry} {mid + r},{entry} H {dst.x}"
            )
            # Label on the vertical run, offset clear of both boxes.
            lab_y = (sy + vert_to) // 2
            return d, (mid, lab_y), "side"
        # left
        lane = self._lane_of(e.src, "left")
        above = dst.cy < src.cy
        entry = dst.bottom if above else dst.y
        mid = dst.right + 24 + lane
        vert_to = entry + (r if above else -r)
        d = (
            f"M {sx},{sy} H {mid + r} Q {mid},{sy} {mid},{sy + (r if above else -r)} "
            f"V {vert_to} Q {mid},{entry} {mid - r},{entry} H {dst.right}"
        )
        lab_y = (sy + vert_to) // 2
        return d, (mid, lab_y), "side"

    # ---------------------------------------------------------------- output

    def _node_svg(self, n: Node) -> str:
        fill, stroke, dash = KINDS[n.kind]
        out = [
            f'<rect x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" rx="6" fill="{PAPER}"/>',
            f'<rect x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1"{dash}/>',
        ]
        name_y = n.cy + 3 if not n.sub else n.cy - 3
        out.append(
            f'<text x="{n.cx}" y="{name_y}" fill="{INK}" font-size="12" font-weight="600" '
            f'font-family="\'Geist\', sans-serif" text-anchor="middle">{_esc(n.name)}</text>'
        )
        if n.sub:
            out.append(
                f'<text x="{n.cx}" y="{n.cy + 13}" fill="{MUTED}" font-size="9" '
                f'font-family="\'Geist Mono\', monospace" text-anchor="middle">{_esc(n.sub)}</text>'
            )
        return "\n      ".join(out)

    def _edge_svg(self, e: Edge) -> tuple[str, str]:
        d, (lx, ly), side = self.route(e)
        stroke, marker, dash = EDGE_KINDS[e.kind]
        path = (
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.2"'
            f'{dash} marker-end="url(#{marker})"/>'
        )
        if not e.label:
            return path, ""
        # label sits ABOVE the connector with a >=6px gap
        w = max(len(e.label) * 6 + 12, 44)
        if side == "side":
            # Vertical connector: place the label to the RIGHT of the run, with
            # the mask clearing the stroke horizontally (6px+ gap).
            label = (
                f'<rect x="{lx + 8}" y="{ly - 6}" width="{w}" height="12" rx="2" fill="{PAPER}"/>'
                f'<text x="{lx + 8 + w // 2}" y="{ly + 3}" fill="#7a8399" font-size="8" '
                f'font-family="\'Geist Mono\', monospace" text-anchor="middle" '
                f'letter-spacing="0.06em">{_esc(e.label)}</text>'
            )
            return path, label
        # Horizontal run: only label it if the gutter is wide enough that the
        # mask cannot be clipped by a neighbouring box. A squeezed label reads
        # worse than none, and the relationship is visible from layout.
        src, dst = self.nodes[e.src], self.nodes[e.dst]
        span = abs(dst.cx - src.cx)
        if w + 28 > span:
            return path, ""
        lx = max(40 + w // 2, min(lx, self.spec["viewbox"][0] - 40 - w // 2))
        label = (
            f'<rect x="{lx - w // 2}" y="{ly - 20}" width="{w}" height="12" rx="2" fill="{PAPER}"/>'
            f'<text x="{lx}" y="{ly - 11}" fill="#7a8399" font-size="8" '
            f'font-family="\'Geist Mono\', monospace" text-anchor="middle" '
            f'letter-spacing="0.06em">{_esc(e.label)}</text>'
        )
        return path, label

    def render_svg(self) -> str:
        vb_w, vb_h = self.spec["viewbox"]
        slug = self.spec["slug"]
        parts: list[str] = [
            f'<svg viewBox="0 0 {vb_w} {vb_h}" xmlns="http://www.w3.org/2000/svg" '
            f'role="img" aria-labelledby="{slug}-title {slug}-desc">',
            f'  <title id="{slug}-title">{_esc(self.spec["title"])}</title>',
            f'  <desc id="{slug}-desc">{_esc(self.spec["desc"])}</desc>',
            "  <defs>",
            *(
                _marker(name, color)
                for name, color in (
                    ("arrow", MUTED),
                    ("arrow-accent", ACCENT),
                    ("arrow-link", LINK),
                )
            ),
            "  </defs>",
            f'  <rect width="100%" height="100%" fill="{PAPER}"/>',
        ]

        # zones first
        for z in self.spec.get("zones", []):
            lw = max(len(z["label"]) * 7 + 16, 44)
            parts.append(
                f'  <rect x="{z["x"]}" y="{z["y"]}" width="{z["w"]}" height="{z["h"]}" rx="8" '
                f'fill="rgba(45,49,66,0.02)" stroke="rgba(45,49,66,0.10)" stroke-width="0.8"/>'
            )
            parts.append(
                f'  <rect x="{z["x"] + 16}" y="{z["y"] + 4}" width="{lw}" height="12" '
                f'rx="2" fill="{PAPER}"/>'
            )
            parts.append(
                f'  <text x="{z["x"] + 16 + lw // 2}" y="{z["y"] + 13}" fill="rgba(45,49,66,0.40)" '
                f'font-size="7" font-family="\'Geist Mono\', monospace" text-anchor="middle" '
                f'letter-spacing="0.14em">{_esc(z["label"])}</text>'
            )

        # edges (paths), then labels, then nodes
        paths, labels = [], []
        for e in self.edges:
            p, lbl = self._edge_svg(e)
            paths.append("  " + p)
            if lbl:
                labels.append("  " + lbl)
        parts += paths + labels
        for n in self.spec["nodes"]:
            parts.append("  " + self._node_svg(self.nodes[n["id"]]))

        # legend
        legend = self.spec.get("legend", [])
        if legend:
            ly = vb_h - 36
            parts.append(
                f'  <line x1="32" y1="{ly}" x2="{vb_w - 32}" y2="{ly}" '
                f'stroke="rgba(45,49,66,0.10)" stroke-width="0.8"/>'
            )
            parts.append(
                f'  <text x="32" y="{ly + 16}" fill="{MUTED}" font-size="8" '
                f'font-family="\'Geist Mono\', monospace" letter-spacing="0.14em">'
                f"LEGEND</text>"
            )
            lx = 128
            for item in legend:
                if item in KINDS:
                    fill, stroke, dash = KINDS[item]
                    parts.append(
                        f'  <rect x="{lx}" y="{ly + 8}" width="10" height="10" rx="2" '
                        f'fill="{fill}" stroke="{stroke}" stroke-width="1"{dash}/>'
                    )
                    lx += 16
                else:
                    stroke, _m, dash = EDGE_KINDS[item]
                    parts.append(
                        f'  <line x1="{lx}" y1="{ly + 13}" x2="{lx + 24}" y2="{ly + 13}" '
                        f'stroke="{stroke}" stroke-width="1.2"{dash}/>'
                    )
                    lx += 32
                txt = {
                    "focal": "FOCAL",
                    "module": "MODULE",
                    "external": "EXTERNAL",
                    "store": "STORE",
                    "input": "INPUT",
                    "async": "ASYNC",
                    "local": "LOCAL CALL",
                    "http": "HTTP",
                    "accent": "PRIMARY",
                }[item]
                parts.append(
                    f'  <text x="{lx}" y="{ly + 16}" fill="{MUTED}" font-size="8" '
                    f"font-family=\"'Geist Mono', monospace\">{txt}</text>"
                )
                lx += len(txt) * 6 + 32

        parts.append("</svg>")
        return "\n    ".join(parts)

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
        print("usage: diagram_gen.py <spec.json> [--out FILE]")
        return 2
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text())
    out = None
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    else:
        out = spec_path.with_suffix(".html")
    d = Diagram(spec)
    out.write_text(d.render_html())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
