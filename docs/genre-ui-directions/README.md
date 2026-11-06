# Genre UI directions (mockups only)

Three alternative directions for the genre browser that the Home Assistant card and the suno-dl web UI share
(`src/suno_dl/webui_static/suno-genre-card.js`). Refs #95, #113.

**These are mockups.** They are static HTML pages in [`mockups/`](mockups/) rendered in Chromium to the PNGs in
[`screenshots/`](screenshots/). Every track title, style, duration, confidence and cover is **synthetic**: titles come
from a word list and covers are generated SVG gradients (`mockups/shared.js`). No real library data, no photos, no
household images. Nothing here is wired to HA, a conversation agent or the archive, and none of it has been tried by a
real user.

| | Direction | One line | Doc |
|---|---|---|---|
| A | Tuned current layout | Genre chips → detail pane, with a real bottom sheet, sticky filter, laptop and 3-pane desktop variants | [a-tuned-current.md](a-tuned-current.md) |
| B | Genre map | Treemap sized by track count; drag a tile onto another to merge, split from the tile; progress lives on the tile | [b-genre-map.md](b-genre-map.md) |
| C | Review queue (my pick) | A queue for low-confidence / Uncategorised songs with accept / reject / undo and a "what would change" panel | [c-review-queue.md](c-review-queue.md) |

## Rules every direction keeps

- The HA card and the web UI stay **one** UI (same JS module; the web UI is the card in `standalone` mode).
- Clicking a genre opens a **detail pane** (right on desktop, bottom sheet on mobile) with stats and the actions
  (Split, Edit). There is no Split button on every chip or row.
- **SUNO verbatim:** titles and artists are shown exactly as stored (whitespace kept, long ones truncated visually with
  the full text in a tooltip / accessible name), never trimmed, re-cased or rewritten. The mock data only uses
  invented titles, but the markup treats them as opaque text.
- Nothing hardcodes a host, IP or machine name; remote options stay optional and configurable.
- Long actions show the same states as today's card: a working line with progress and Cancel, then either a result or
  an error banner naming the cause with Retry / Copy details / Dismiss. A failed or cancelled action changes nothing.

## Screenshots (dark unless named `light`)

Each direction has 360x800, 1280x720 and 1920x1080 dark shots plus one light shot at 1280x720 (A also has an extra
error shot). Files in `screenshots/`:

- A: `a-tuned-360x800-dark`, `a-tuned-1280x720-dark-working`, `a-tuned-1280x720-dark-error`,
  `a-tuned-1920x1080-dark-split-preview`, `a-tuned-1280x720-light`
- B: `b-map-360x800-dark-working`, `b-map-1280x720-dark-drag-merge`, `b-map-1920x1080-dark-error`,
  `b-map-1280x720-light`
- C: `c-queue-360x800-dark-review`, `c-queue-1280x720-dark-commit`, `c-queue-1920x1080-dark-error`,
  `c-queue-1280x720-light-review`

Regenerate with the page URLs `mockups/<file>.html?state=<idle|working|preview|error|dragging|review>&theme=<dark|light>`
at the viewport you want; the shots were taken with Playwright Chromium.

## How to compare

| | A | B | C |
|---|---|---|---|
| Change vs today | small | large | medium (new screen + small hooks in the card) |
| Fixes a known pain | mobile + tablet layout | spotting lopsided genres, merging | the "N song(s) not classified" leftovers and low-confidence songs |
| Needs new backend | no | merge endpoint / atomic multi-rename | per-song confidence + suggestions from the classifier |
| Risk | low | drag accessibility, touch precision | confidence numbers must be real, not decorative |
