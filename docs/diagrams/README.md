# Diagrams

Visual documentation for `suno-download-cli`. Every diagram is derived from the
repository source — component names, module names, and flows are taken from
actual files, not invented. See [INVENTORY.md](INVENTORY.md) for the full
inventory, sources, and stated assumptions.

## Index

| Diagram | Type | Audience | Answers |
| --- | --- | --- | --- |
| [01-hero](01-hero.html) | Architecture (overview) | everyone | What is this, who uses it, what does it touch? |
| [02-architecture](02-architecture.html) | Architecture (zoned) | developers | How do the modules fit together? |
| [03-deployment](03-deployment.html) | Deployment | operators | Where does this actually run? |
| [04-sequence-happy](04-sequence-happy.html) | Sequence | developers | What happens, in order, when I run a download? |
| [05-integrations](05-integrations.html) | Integration map | developers | What external systems, and how do they fail? |
| [06-dataflow](06-dataflow.html) | Data flow | developers / security | How does untrusted content become a safe file? |
| [07-security](07-security.html) | Trust boundaries | security / reviewers | What are the three defences? |
| [08-state-machine](08-state-machine.html) | State machine | developers | What outcomes can a track have? |
| [09-dependency](09-dependency.html) | Dependency graph | new contributors | Which module imports which? |
| [10-operations](10-operations.html) | Process | operators | How is it tested and released? |

Rendered PNGs are in [`renders/`](renders/) for quick preview.

## How to view

Open any `.html` directly in a browser. Each file is fully self-contained
(inline SVG + CSS; only Google Fonts is external) and renders correctly without
JavaScript.

## How to edit or regenerate

Diagrams are **generated**, not hand-drawn. The generator computes orthogonal
routing, label margins, and attach points so the design-system connector rules
cannot be violated by hand.

```
docs/diagrams/specs/*.json     declarative source (nodes, edges, actors)
        │
        ├─ diagram_gen.py    → box-and-arrow types (architecture, deployment,
        │                       integration, data-flow, security, dependency, ops)
        ├─ sequence_gen.py   → sequence type (lifelines, messages, fragments)
        └─ state_gen.py      → state machine (states, guarded transitions)
        │
        ▼
docs/diagrams/*.html           self-contained output
        │
        └─ render_diagrams.py → renders/ *.png (Playwright screenshot)
```

Regenerate and validate everything:

```bash
bash scripts/regen_diagrams.sh
```

The generators escape XML in all text, so labels like `<base>.m4a` are safe,
and they drop a connector label rather than clip it into a neighbouring box.

Validate the accessible-SVG contract and file safety:

```bash
python3 ~/.config/opencode/skills/diagram-design/scripts/self_check.py docs/diagrams/*.html
```

## Design system

Follows the `diagram-design` skill's default editorial skin:

| Role | Value |
| --- | --- |
| paper | `#f5f5f5` white-smoke |
| ink | `#2d3142` jet-black |
| muted | `#4f5d75` blue-slate |
| accent | `#eb6c36` atomic-tangerine |
| link (HTTP) | `#2e5aa8` |

Type: Instrument Serif (title), Geist (names), Geist Mono (technical labels).
Coral is reserved for 1–2 focal elements per diagram; every diagram has a legend
strip at the bottom.

## Source references

| Diagram | Primary sources |
| --- | --- |
| 01-hero | `src/suno_dl/cli.py`, `README.md`, `src/suno_dl/urls.py` |
| 02-architecture | all `src/suno_dl/*.py` |
| 03-deployment | `pyproject.toml`, `scripts/suno-ci-local.{service,timer}`, `.github/workflows/` |
| 04-sequence-happy | `cli.py::_run`, `discovery.py`, `downloader.py::download_tracks`, `fetcher.py` |
| 05-integrations | `fetcher.py`, `urls.py::ALLOWED_MEDIA_HOSTS`, `docs/troubleshooting.md` |
| 06-dataflow | `parser.py`, `urls.py`, `naming.py`, `downloader.py` |
| 07-security | `urls.py::is_allowed_media_url`, `naming.py::sanitize_component`, `urls.py::redact_url`, `downloader.py::_is_contained`, `SECURITY.md` |
| 08-state-machine | `downloader.py::_download_single` return `Literal[...]` |
| 09-dependency | `grep '^from \.' src/suno_dl/*.py` |
| 10-operations | `scripts/ci-local.sh`, `Makefile`, `.github/workflows/release.yml` |

## Assumptions and limitations

- **No peer-discovery / CLM diagrams.** That capability does not exist in this
  repository. Nothing here depicts it.
- **Discovery is shown as one path.** The `--browser` branch is the default;
  the `--no-browser` fallback is not a separate diagram.
- **Signed-URL TTL is not stated** — the exact expiry is not knowable from the
  repository.
- **Sequence timing is illustrative.** Vertical spacing is not to scale; the
  diagram shows order, not duration.
- **State names match the code literals** (`downloaded`, `skipped_exists`,
  `failed`), not invented labels.
- **Deployment shows a workstation + local CI timer**, which is the documented
  setup here; there is no server component to depict.
