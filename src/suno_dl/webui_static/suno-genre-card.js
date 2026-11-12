/**
 * Suno Genre Browser v6 — shared UI for the HA card and the suno-dl webui.
 *
 * Genre detail pane (right on wide cards, bottom sheet with peek state on
 * narrow ones), nested subtier chips, sort + quick filters, track cards with
 * play / duration / date / inline lyrics, Copy ▾ and ⋯ menus, keyboard.
 *
 * Install: copy this folder into <HA config>/www/suno-genres/ and register
 * /local/suno-genres/suno-genre-card.js as a Lovelace dashboard resource
 * (type: JavaScript module). suno-dl's webui serves this same file in
 * read-only `standalone` mode.
 */
import {
  AGENT_TIMEOUT_MS,
  DEFAULT_AGENT_ID,
  MAX_SUBTIER_ASSIGNMENT_CHUNK,
  MIN_SPLIT_SONGS,
  OpCancelledError,
  ReplyParseError,
  apiErrorFrom,
  assertAgentOk,
  applySplitPreview,
  childShares,
  classifierMix,
  clipboardContext,
  clipsInCategory,
  controlsLocked,
  countAssignments,
  createOp,
  ensureGenres,
  extractSpeech,
  extractSpeechStrict,
  failOp,
  failureDetails,
  filterClips,
  fmtDate,
  fmtDuration,
  fmtStamp,
  honestCounts,
  isRefusalText,
  leafNames,
  mergeAssignments,
  normalizeSubcategories,
  opCancel,
  opCheck,
  opPercent,
  opStatusLine,
  opUpdate,
  packAiContext,
  parentMap,
  parseBpm,
  parseJsonReply,
  quickCounts,
  quickFilter,
  recount,
  redactSecrets,
  replySnippet,
  shortChildName,
  sortClips,
  splitReason,
  strictJsonPrompt,
  subtierParent,
  summariseContext,
  topChipRows,
  withTimeout,
} from "./suno-genre-core.js?v=7";

const SONGS_PER_PAGE = 50;

const DEFAULTS = {
  title: "Suno archive",
  agent_id: DEFAULT_AGENT_ID,
  apply_url: "",
  undo_url: "",
  reclassify_url: "",
  lyrics_url: "",
  // No remote host is ever defaulted: the webui passes relative URLs, HA passes its own config.
  tracks_url: "",
  clips_url: "/local/suno-genres/clips.json",
  genres_url: "/local/suno-genres/genres.json",
  library_url: "",
  songs_url: "",
  ingress_app: "",
  // Optional: where to POST failed long actions so the full cause is logged server-side.
  client_error_url: "",
  agent_timeout_s: AGENT_TIMEOUT_MS / 1000,
  standalone: false,
};

const CSS_ESCAPE = (s) =>
  typeof CSS !== "undefined" && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");

const PLAY_SVG = '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 0.5l8 4.5-8 4.5z"/></svg>';
const PAUSE_SVG =
  '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5 0.5h2.6v9H1.5zM5.9 0.5h2.6v9H5.9z"/></svg>';
const SEARCH_SVG =
  '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="7" cy="7" r="5"/><path d="M11 11l4 4"/></svg>';

const STYLES = `
:host {
  display: block;
  --sg-blue: #2563eb;
  --sg-outline: #60a5fa;
  /* The card paints its own dark surface so every text colour below is
     measured against a known background, whatever the HA theme is. */
  --sg-card: #141414;
  --sg-line: #262626;
  --sg-muted: #9a9a9a;
  --sg-soft: #9a9a9a;
  font-family: var(--ha-font-family-body, Inter, system-ui, Roboto, sans-serif);
  color: #e1e1e1;
}
:host, .wrap, .wrap * { box-sizing: border-box; }
.wrap { padding: 12px; display: flex; flex-direction: column; gap: 10px; container: wrap / inline-size; max-width: 1320px; margin: 0 auto; background: #0b0b0b; color: #e1e1e1; border-radius: 14px; }
.hd { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
h2 { margin: 0; font-size: 1.15rem; font-weight: 600; }
.counts { font-size: .8rem; color: #a8a8a8; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.counts b { color: #ddd; font-weight: 600; }
.counts .blk { color: #fbbf24; }
.admin { margin-left: auto; display: flex; flex-wrap: wrap; gap: 6px; }
button { font: inherit; }
.ghost { height: 30px; padding: 0 10px; border-radius: 8px; border: 1px solid #3a3a3a; background: transparent; color: #ddd; cursor: pointer; font-size: .8rem; }
.ghost:disabled { opacity: .45; cursor: default; }

:where(button, a, input, textarea, [tabindex]):focus-visible { outline: 2px solid var(--sg-outline); outline-offset: 2px; }
.search:focus-within { border-color: var(--sg-outline); box-shadow: 0 0 0 1px var(--sg-outline); }
.pane:focus { outline: none; }
.chip, .qf, .seg button, .lnk, .splitb button, .more, .ghost { flex: none; white-space: nowrap; }
/* never smaller than 24x24 CSS px with a mouse (WCAG 2.5.8); 44x44 for touch / narrow cards below */
.lnk, .splitb button, .more, .ghost, .chip, .seg button, .qf, .mini, .kid, .menu button, .pane-close, .play, button.act { min-height: 24px; min-width: 24px; }
.sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }

.layout { display: flex; gap: 14px; align-items: flex-start; }
.main {
  container: main / inline-size;
  flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 10px;
  padding: 14px; border-radius: 12px; background: var(--sg-card); border: 1px solid #2a2a2a;
}
.main > * { flex-shrink: 0; }
.stick { display: flex; flex-direction: column; gap: 8px; background: var(--sg-card); }
.chips { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 12px;
  border-radius: 15px; border: 1px solid #333; background: #1f2937; color: #ccc;
  font-size: .8rem; cursor: pointer; white-space: nowrap;
}
.chip .n { font-size: .7rem; opacity: .65; font-variant-numeric: tabular-nums; }
.chip.active { background: var(--sg-blue); color: #fff; border-color: transparent; }
.chip.selected { color: #eef; border-color: var(--sg-outline); box-shadow: 0 0 0 1px var(--sg-outline); }
.chip.within { border-color: #3b5b8f; border-style: dashed; color: #dbe7ff; }
.chips.sub { padding: 6px 10px; margin-left: 24px; border-left: 2px solid #2b3647; border-radius: 0 8px 8px 0; background: #10151d; }
.chips.sub .chip { height: 26px; font-size: .77rem; background: #151c27; border-color: #2b3647; }
.chips.sub .chip.selected { border-color: var(--sg-outline); }
.sublbl { font-size: .72rem; color: #6b7c93; margin-right: 2px; white-space: nowrap; }
.more-chips { display: none; }

.filt { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; padding-top: 6px; border-top: 1px solid #1f1f1f; }
.search {
  flex: 0 1 300px; min-width: 180px; display: flex; align-items: center; gap: 8px; height: 32px; padding: 0 10px;
  border: 1px solid #2a2a2a; border-radius: 8px; background: #0f0f0f; color: var(--sg-soft);
}
.search input { flex: 1 1 auto; min-width: 0; border: none; background: transparent; color: #ddd; font: inherit; font-size: .8rem; outline: none; height: 30px; padding: 0; }
.kbd { display: inline-block; min-width: 18px; padding: 0 5px; height: 18px; line-height: 16px; border: 1px solid #444; border-bottom-width: 2px; border-radius: 4px; font: 11px "JetBrains Mono", ui-monospace, monospace; color: #bbb; text-align: center; background: #151515; }
.seg { display: inline-flex; border: 1px solid #333; border-radius: 8px; overflow: hidden; }
.seg button { border: none; border-right: 1px solid #333; background: transparent; color: #aaa; padding: 5px 10px; font-size: .77rem; cursor: pointer; }
.seg button:last-child { border-right: none; }
.seg button.on { background: #1e293b; color: #e5edff; }
.qf { display: inline-flex; align-items: center; gap: 5px; height: 26px; padding: 0 10px; border-radius: 13px; border: 1px dashed #3a3a3a; background: transparent; color: #aaa; font-size: .77rem; cursor: pointer; }
.qf b { color: #ddd; font-weight: 600; }
.qf.warn b { color: #fbbf24; }
.qf.on { border-style: solid; border-color: var(--sg-outline); background: #172033; color: #dbe7ff; }
.shown { margin-left: auto; font-size: .77rem; color: var(--sg-soft); }

.list { display: flex; flex-direction: column; gap: 8px; }
.song {
  position: relative; display: grid; grid-template-columns: 34px minmax(0, 1fr) auto;
  column-gap: 12px; row-gap: 3px; padding: 10px 12px; border-radius: 10px;
  background: #101010; border: 1px solid #222;
}
.song.foc { border-color: #3b5b8f; }
.song.playing { border-color: #2a4a7a; background: #0f141c; }
.play {
  grid-column: 1; grid-row: 1 / span 3; align-self: start; width: 34px; height: 34px; border-radius: 50%;
  background: #1e293b; border: 1px solid #334155; display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0;
}
.play svg { width: 12px; height: 12px; fill: #93c5fd; margin-left: 1px; }
.play.on { background: var(--sg-blue); border-color: transparent; }
.play.on svg { fill: #fff; margin-left: 0; }
.play.off { background: #161616; border-style: dashed; border-color: #3a3a3a; cursor: default; }
.play.off svg { fill: #555; }
.tl { grid-column: 2; grid-row: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
.acts { grid-column: 3; grid-row: 1; display: flex; align-items: center; gap: 6px; }
.meta { grid-column: 2 / -1; grid-row: 2; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: .74rem; color: var(--sg-muted); }
.style { grid-column: 2 / -1; grid-row: 3; font-size: .77rem; color: var(--sg-soft); display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; word-break: break-word; }
.full { grid-column: 2 / -1; display: grid; grid-template-columns: 1.1fr 1fr; gap: 14px; margin-top: 6px; padding: 10px 12px; border-radius: 8px; background: #0c0c0c; border: 1px solid #1d1d1d; }
.full h4 { margin: 0 0 5px; font-size: .68rem; color: #888; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
.full p, .full pre { margin: 0; font-size: .77rem; color: #a3a3a3; line-height: 1.5; white-space: pre-wrap; word-break: break-word; font-family: inherit; }
.full pre { max-height: 260px; overflow: auto; }
.prog { grid-column: 2 / -1; height: 3px; border-radius: 2px; background: #1f2937; overflow: hidden; margin-top: 4px; }
.prog i { display: block; height: 100%; width: 0; background: var(--sg-outline); }
.title { color: #93c5fd; text-decoration: none; font-weight: 600; font-size: .86rem; white-space: pre; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.title, .style, .meta, .full, .pane, .song {
  -webkit-user-select: text; -moz-user-select: text; user-select: text; -webkit-touch-callout: default;
}
.badge { font-size: .66rem; padding: 1px 6px; border-radius: 5px; background: #262626; color: #aaa; white-space: nowrap; }
.badge.cat { background: rgba(59, 130, 246, .16); color: #93c5fd; }
.badge.pin { background: rgba(156, 39, 176, .25); color: #ce93d8; }
.badge.warn { background: rgba(245, 158, 11, .12); color: #fbbf24; }
.meta .badge.mob { display: none; }
.lnk { border: none; background: transparent; color: #93c5fd; font-size: .74rem; cursor: pointer; padding: 0; }
.splitb { display: inline-flex; border: 1px solid #444; border-radius: 7px; overflow: hidden; height: 26px; }
.splitb button { border: none; background: transparent; color: #ddd; font-size: .75rem; padding: 0 9px; cursor: pointer; }
.splitb button + button { border-left: 1px solid #444; padding: 0 6px; color: #aaa; }
.more { width: 26px; height: 26px; border: 1px solid #444; border-radius: 7px; background: transparent; color: #bbb; font-size: 15px; line-height: 1; cursor: pointer; padding: 0; }
.more.on, .splitb.on { border-color: var(--sg-outline); }
.menu {
  position: absolute; right: 10px; top: 40px; width: 230px; z-index: 20; padding: 5px;
  background: #1b1b1b; border: 1px solid #3a3a3a; border-radius: 10px; box-shadow: 0 12px 30px rgba(0, 0, 0, .6);
}
.menu button { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 9px; border: none; border-radius: 6px; background: transparent; color: #ddd; font-size: .8rem; text-align: left; cursor: pointer; }
.menu button:hover:not(:disabled) { background: #262f3d; }
.menu button:disabled { color: #666; cursor: default; }
.menu button span { color: var(--sg-soft); font-size: .7rem; }
.menu hr { border: none; border-top: 1px solid #2c2c2c; margin: 4px 2px; }
.menu .mlist { max-height: 230px; overflow: auto; }
.menu .mreason { font-size: .7rem; color: var(--sg-soft); padding: 4px 9px 6px; line-height: 1.4; }
.menu button.cur { color: #93c5fd; }

.pane {
  position: sticky; top: calc(var(--header-height, 56px) + 12px); align-self: flex-start;
  max-height: calc(100vh - var(--header-height, 56px) - 24px); overflow: auto;
  flex: 0 0 340px; width: 340px; display: flex; flex-direction: column; gap: 12px;
  padding: 16px; border-radius: 12px; background: #171717; border: 1.5px solid var(--sg-blue);
}
.pane-close { position: absolute; top: 12px; right: 12px; width: 26px; height: 26px; border-radius: 7px; border: 1px solid #444; background: transparent; color: #aaa; cursor: pointer; font-size: .75rem; padding: 0; }
.pane-title { font-size: 1rem; font-weight: 600; color: #eee; padding-right: 36px; }
.pane-meta { font-size: .75rem; color: var(--sg-soft); margin-top: -8px; }
.handle, .peekrow { display: none; }
.pfull { display: flex; flex-direction: column; gap: 12px; }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 11px 12px; border-radius: 8px; background: #101010; border: 1px solid #2a2a2a; }
.stat .lbl { font-size: .7rem; color: #9ad; display: block; }
.stat .val { font-size: 1.3rem; font-weight: 600; color: #fff; font-variant-numeric: tabular-nums; }
.sec { font-size: .68rem; color: #9a9a9a; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.mix { display: flex; flex-wrap: wrap; gap: 6px; }
.mix .badge { font-size: .7rem; padding: 2px 7px; }
.bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: #222; }
.bar i { display: block; height: 100%; }
.kids { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
.kid { display: grid; grid-template-columns: 10px 1fr auto; gap: 8px; align-items: center; font-size: .8rem; color: #ccc; cursor: pointer; border: none; background: transparent; text-align: left; padding: 2px 0; }
.kid i { width: 10px; height: 10px; border-radius: 3px; }
.kid .c { color: var(--sg-soft); font-variant-numeric: tabular-nums; font-size: .75rem; }
.kid.cur { color: #93c5fd; }
.ask { display: flex; flex-direction: column; gap: 8px; padding: 10px; border-radius: 10px; background: #121a29; border: 1px solid #23324d; }
.ask .sec { color: #8fb1e8; margin: 0; }
textarea { width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid #2a3a57; background: #0d1320; color: inherit; font: inherit; font-size: .8rem; resize: vertical; }
.row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
button.act { height: 32px; padding: 0 12px; border-radius: 8px; border: none; cursor: pointer; font-size: .8rem; background: var(--sg-blue); color: #fff; }
button.act.secondary { background: #1f2937; color: #eee; border: 1px solid #444; }
button.act:disabled { opacity: .45; cursor: default; }
.op { position: sticky; top: var(--header-height, 0px); z-index: 10; display: flex; flex-direction: column; gap: 6px; margin: 8px 0; padding: 10px 12px; border-radius: 10px; background: #14213a; border: 1px solid #2f4b80; font-size: .82rem; }
.op .oprow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.op .opline { flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; }
.op .spin { width: 16px; height: 16px; flex: none; border-radius: 50%; border: 2px solid #3b5a96; border-top-color: #9ecbff; animation: sgspin .8s linear infinite; }
@keyframes sgspin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .op .spin { animation-duration: 3s; } }
.op .bar { height: 6px; border-radius: 4px; background: #0d1320; overflow: hidden; }
.op .bar i { display: block; height: 100%; background: #4c8dff; transition: width .25s ease; }
.pane .op { position: static; margin: 0; }
.op.failed { background: #2a1416; border-color: #8a2f36; }
.op.cancelled { background: #1d1d1d; border-color: #555; }
.op .ophd { font-size: .86rem; }
.op code { font-size: .78rem; background: #00000055; padding: 1px 5px; border-radius: 4px; }
.mini { height: 26px; padding: 0 9px; border-radius: 7px; border: 1px solid #444; background: transparent; color: inherit; cursor: pointer; font-size: .75rem; }
.mini.danger { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
.reason { font-size: .74rem; color: #a8a8a8; background: #1c1c1c; border-left: 2px solid #555; padding: 6px 9px; border-radius: 0 6px 6px 0; line-height: 1.45; }
.keys { display: flex; flex-wrap: wrap; gap: 6px 12px; font-size: .74rem; color: var(--sg-soft); border-top: 1px solid #262626; padding-top: 10px; }
pre.out { margin: 0; white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow: auto; padding: 10px; border-radius: 8px; background: #0d1320; font-size: .8rem; }
.panel { display: flex; flex-direction: column; gap: 8px; padding: 10px; border-radius: 10px; background: #1a1a1a; border: 1px solid #2a2a2a; }
.panel h3 { margin: 0; font-size: .85rem; }
.subcats { margin: 0; padding-left: 18px; font-size: .8rem; }
.subcats .n { color: var(--sg-soft); font-size: .75rem; }
.note { font-size: .74rem; color: var(--sg-soft); line-height: 1.45; }
.err { padding: 10px 12px; border-radius: 10px; font-size: .8rem; color: var(--error-color, #db4437); background: rgba(219, 68, 55, .12); border: 1px solid rgba(219, 68, 55, .35); word-break: break-word; }
.ok { color: var(--success-color, #43a047); font-size: .8rem; }
.empty { padding: 16px 4px; color: var(--sg-soft); }

@container wrap (max-width: 700px) {
  .wrap { padding: 8px; }
  .admin { margin-left: 0; }
  .layout { flex-direction: column; align-items: stretch; }
  .main { padding: 10px; }
  .stick {
    position: sticky; top: var(--header-height, 56px); z-index: 8;
    margin: -10px -10px 0; padding: 10px 0 8px; border-bottom: 1px solid #1f1f1f;
    border-radius: 12px 12px 0 0; box-shadow: 0 6px 12px rgba(0, 0, 0, .35);
  }
  /* #133: wrap instead of hiding chips behind an invisible sideways scroll. */
  .chips { flex-wrap: wrap; padding: 0 10px; }
  .chips.top { max-height: 104px; overflow: hidden; }
  .chips.top.open { max-height: none; }
  /* No min-height here: it would outrank the 44px touch rule below. */
  .chips .chip { max-width: 100%; height: auto; padding-top: 4px; padding-bottom: 4px; white-space: normal; text-align: left; }
  .chips.sub .chip { min-height: 26px; }
  .more-chips {
    display: inline-flex; align-items: center; align-self: flex-start; margin: 0 10px;
    height: 26px; padding: 0 10px; border-radius: 13px; border: 1px dashed #3a3a3a;
    background: transparent; color: #9ad; font-size: .77rem; cursor: pointer;
  }
  .more-chips[hidden] { display: none; }
  .chips.sub { margin-left: 0; padding: 0 10px 0 14px; border: none; border-radius: 0; background: transparent; }
  .search { flex: 1 1 100%; }
  .shown { display: none; }
  .keys { display: none; }
  .pane {
    position: sticky; top: auto; bottom: 0; z-index: 9; align-self: stretch; width: 100%; flex: 0 0 auto;
    max-height: 72vh; border-radius: 18px 18px 0 0; border-bottom: none; padding: 8px 14px 14px;
    box-shadow: 0 -14px 30px rgba(0, 0, 0, .65);
  }
  .pane-close { top: 18px; }
  .pane-title { padding-right: 56px; }
  .handle { display: block; flex: none; width: 44px; height: 18px; margin: 0 auto -4px; cursor: grab; touch-action: none; position: relative; border: none; background: transparent; padding: 0; }
  .handle::after { content: ""; position: absolute; left: 2px; right: 2px; top: 7px; height: 4px; border-radius: 2px; background: #555; }
  .pane.peek .pfull { display: none; }
  .pane.peek .peekrow { display: flex; }
  .peekrow { flex-direction: column; gap: 9px; }
  .minis { display: flex; align-items: baseline; gap: 14px; font-size: .74rem; color: #9ad; }
  .minis b { color: #fff; font-size: .95rem; margin-left: 4px; }
  .minis .m2 { margin-left: auto; color: #888; }
  .peekbtns { display: flex; gap: 7px; }
  .peekbtns button { flex: 1 1 0; height: 34px; padding: 0 6px; }
}

@container main (max-width: 600px) {
  .song { grid-template-columns: 32px minmax(0, 1fr) auto; column-gap: 10px; padding: 9px 10px; }
  .play { width: 32px; height: 32px; }
  .tl { grid-column: 2 / -1; }
  .tl .badge { display: none; }
  .title { white-space: pre-wrap; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; line-height: 1.3; }
  .meta { grid-column: 2 / -1; }
  .meta .badge.mob { display: inline; }
  .acts { grid-column: 2 / -1; grid-row: 4; justify-content: flex-end; flex-wrap: wrap; } /* its own row: 44px buttons never squeeze the badges */
  .full { grid-column: 1 / -1; grid-template-columns: 1fr; }
  .full > div:first-child { order: 2; }
  .menu { top: calc(100% - 8px); }
}
@media (pointer: coarse) {
  .ghost, .chip, .chips.sub .chip, .seg button, .qf, .more, .more-chips, .splitb, .splitb button, .lnk, .pane-close, button.act, .mini, .kid, .menu button { min-height: 44px; }
  .search { height: 44px; } .search input { height: 42px; }
  .play { width: 44px; height: 44px; } .more, .pane-close, .splitb button + button { min-width: 44px; }
  .lnk { min-width: 44px; padding: 0 8px; }
  .seg button, .chip, .more-chips, button.act, .ghost, .qf, .mini, .kid { min-width: 44px; }
  .title { padding: 13px 0; min-height: 44px; }
  .song { grid-template-columns: 44px minmax(0, 1fr) auto; }
}
@container wrap (max-width: 700px) {
  .ghost, .chip, .chips.sub .chip, .seg button, .qf, .more, .more-chips, .splitb, .splitb button, .lnk, .pane-close, button.act, .mini, .kid, .menu button { min-height: 44px; }
  .search { height: 44px; } .search input { height: 42px; }
  .play { width: 44px; height: 44px; } .more, .pane-close, .splitb button + button { min-width: 44px; }
  .lnk { min-width: 44px; padding: 0 8px; }
  .seg button, .chip, .more-chips, button.act, .ghost, .qf, .mini, .kid { min-width: 44px; }
  .handle { height: 44px; flex: none; margin: -6px auto -22px; } .handle::after { top: 19px; }
  .peekbtns button { height: 44px; }
}
@container main (max-width: 600px) {
  .song { grid-template-columns: 44px minmax(0, 1fr) auto; }
  .title { padding: 13px 0; min-height: 44px; }
/* The standalone host alone opts in; HA retains its pre-MVP styling. */
/* Standalone-only warning and information labels. */
:host([data-suno-theme]) .counts .blk,
:host([data-suno-theme]) .qf.warn b,
:host([data-suno-theme]) .badge.warn{color:var(--sg-theme-warn)}
:host([data-suno-theme]) .stat .lbl,
:host([data-suno-theme]) .minis{color:var(--sg-theme-accent)}
:host([data-suno-theme]) .badge.cat{
 color:var(--sg-theme-accent);background:var(--sg-theme-surface)}
:host([data-suno-theme]) .chip.selected{color:var(--sg-theme-text)}
:host([data-suno-theme]) .seg button.on{
 background:var(--sg-theme-raised);color:var(--sg-theme-text);
 box-shadow:inset 0 -3px var(--sg-theme-accent)}

:host([data-suno-theme]){
 color:var(--sg-theme-text,var(--primary-text-color,#e1e1e1));
 --sg-blue:var(--sg-theme-primary,#2563eb);
 --sg-outline:var(--sg-theme-accent,#60a5fa);
 --sg-card:var(--sg-theme-raised,var(--card-background-color,#141414));
 --sg-line:var(--sg-theme-border,#262626);
 --sg-muted:var(--sg-theme-muted,#7a7a7a);
}
:host([data-suno-theme]) .main,
:host([data-suno-theme]) .pane,
:host([data-suno-theme]) .panel{
 background:var(--sg-theme-raised);border-color:var(--sg-theme-border);
 color:var(--sg-theme-text)}
:host([data-suno-theme]) .song,
:host([data-suno-theme]) .full,
:host([data-suno-theme]) .ask,
:host([data-suno-theme]) .menu,
:host([data-suno-theme]) .stats{
 background:var(--sg-theme-surface);border-color:var(--sg-theme-border);
 color:var(--sg-theme-text)}
:host([data-suno-theme]) .chip,
:host([data-suno-theme]) .qf,
:host([data-suno-theme]) .ghost,
:host([data-suno-theme]) .mini,
:host([data-suno-theme]) .splitb,
:host([data-suno-theme]) .seg,
:host([data-suno-theme]) .search,
:host([data-suno-theme]) .play,
:host([data-suno-theme]) .reason,
:host([data-suno-theme]) pre.out{
 background:var(--sg-theme-surface);border-color:var(--sg-theme-border);
 color:var(--sg-theme-text)}
:host([data-suno-theme]) .chips.sub,
:host([data-suno-theme]) .stick{background:var(--sg-theme-raised);
 border-color:var(--sg-theme-border)}
:host([data-suno-theme]) .title,
:host([data-suno-theme]) .lnk,
:host([data-suno-theme]) .pane .sec,
:host([data-suno-theme]) .kid.cur{color:var(--sg-theme-accent)}
:host([data-suno-theme]) .counts,
:host([data-suno-theme]) .style,
:host([data-suno-theme]) .meta,
:host([data-suno-theme]) .note,
:host([data-suno-theme]) .pane-meta,
:host([data-suno-theme]) .sec,
:host([data-suno-theme]) .shown,
:host([data-suno-theme]) .empty,
:host([data-suno-theme]) .mreason,
:host([data-suno-theme]) .subcats .n,
:host([data-suno-theme]) .menu button span{color:var(--sg-theme-muted)}
:host([data-suno-theme]) .counts b,
:host([data-suno-theme]) .pane-title,
:host([data-suno-theme]) .stat .val,
:host([data-suno-theme]) .minis b,
:host([data-suno-theme]) .full p,
:host([data-suno-theme]) .full pre,
:host([data-suno-theme]) .qf b,
:host([data-suno-theme]) .menu button,
:host([data-suno-theme]) .splitb button,
:host([data-suno-theme]) .seg button,
:host([data-suno-theme]) .kid{color:var(--sg-theme-text)}
:host([data-suno-theme]) .search input,
:host([data-suno-theme]) textarea{
 background:var(--sg-theme-surface);color:var(--sg-theme-text);
 border-color:var(--sg-theme-border)}
:host([data-suno-theme]) .song.playing{
 background:var(--sg-theme-raised);border-color:var(--sg-theme-accent)}
:host([data-suno-theme]) .menu button:hover:not(:disabled){
 background:var(--sg-theme-raised)}
:host([data-suno-theme]) .chip.active,
:host([data-suno-theme]) .play.on{background:var(--sg-blue);color:#fff}
:host([data-suno-theme]) .op{background:var(--sg-op-bg);
 color:var(--sg-op-text);border-color:var(--sg-op-border)}
:host([data-suno-theme]) .op.failed{background:var(--sg-op-error-bg);
 color:var(--sg-op-error-text);border-color:var(--sg-op-error-border)}
:host([data-suno-theme]) .op.cancelled{background:var(--sg-op-cancel-bg);
 color:var(--sg-op-cancel-text);border-color:var(--sg-theme-border)}
:host([data-suno-theme]) .op .note{color:inherit}
:host([data-suno-theme]) .op code{background:var(--sg-op-code-bg)}
:host([data-suno-theme]) .op button.act.secondary{
 background:var(--sg-op-secondary-bg);color:var(--sg-op-text);
 border-color:var(--sg-op-border)}
:host([data-suno-theme]) .wrap:focus-visible{
 outline:3px solid var(--sg-theme-accent);outline-offset:2px}
:host([data-suno-theme]) :is(button,a,input,textarea):focus-visible{
 outline:3px solid var(--sg-theme-accent);outline-offset:2px}
:host([data-compact]) .song{padding:7px 10px}
:host([data-compact]) .wrap{gap:7px;padding:8px}
/* Standalone-only reachable touch targets; Home Assistant keeps its CSS. */
:host([data-suno-theme]) .song{
  grid-template-columns:44px minmax(0,1fr) auto}
:host([data-suno-theme]) .play,
:host([data-suno-theme]) .more,
:host([data-suno-theme]) .pane-close{width:44px;height:44px}
:host([data-suno-theme]) .chip,
:host([data-suno-theme]) .chips.sub .chip,
:host([data-suno-theme]) .qf{min-height:44px;height:auto}
:host([data-suno-theme]) .seg button{min-width:44px;min-height:44px}
:host([data-suno-theme]) .search{min-height:44px;height:auto}
:host([data-suno-theme]) .search input{min-height:42px}
:host([data-suno-theme]) .splitb{height:46px}
:host([data-suno-theme]) .splitb button{min-width:44px;min-height:44px}
:host([data-suno-theme]) .menu button{min-height:44px}
:host([data-suno-theme]) .lnk{display:inline-flex;align-items:center;min-height:44px;padding:0 8px}
:host([data-suno-theme]) .more-chips{min-height:44px;height:auto;padding:0 12px}
:host([data-suno-theme]) .pane-title{padding-right:52px}
:host([data-suno-theme]) .peekbtns button{min-height:44px;height:auto}
@container (max-width:700px){
  /* Two columns give 44px controls room at 320px without squeezing titles. */
  :host([data-suno-theme]) .song{
    grid-template-columns:44px minmax(0,1fr)}
  :host([data-suno-theme]) .tl{grid-column:2;grid-row:1}
  :host([data-suno-theme]) .play{grid-row:1 / span 2}
  :host([data-suno-theme]) .acts{
    grid-column:2;grid-row:2;justify-self:start;flex-wrap:wrap;min-width:0}
  :host([data-suno-theme]) .meta{grid-column:1/-1;grid-row:3}
  :host([data-suno-theme]) .style{grid-column:1/-1;grid-row:4}
  :host([data-suno-theme]) .prog{grid-column:1/-1}
  :host([data-suno-theme]) .pane-close{top:8px}
  :host([data-suno-theme]) .handle{
    width:44px;height:44px;min-height:44px;max-height:44px;flex:0 0 44px;
    margin:0 auto -4px}
  :host([data-suno-theme]) .handle::after{top:19px}
}
`;

class SunoGenreCard extends HTMLElement {
  static getStubConfig() {
    return Object.assign({}, DEFAULTS);
  }

  setConfig(config) {
    this._config = Object.assign({}, DEFAULTS, config || {});
    this._activeCategory = "All";
    this._search = "";
    this._page = 1;
    this._pageQuery = "";
    this._livePage = null;
    this._liveKey = "";
    this._focusPage = false;
    this._ingress = null;
    this._liveRequest?.abort();
    this._liveRequest = null;
    this._prompt = "Write the next song in this series (title, style, lyrics)";
    this._aiResponse = "";
    this._aiError = "";
    this._aiLoading = false;
    this._lastCtxNote = "";
    this._ctxForFallback = null;
    this._loadError = "";
    this._loading = true;
    this._clips = [];
    this._genres = ensureGenres(null);
    this._lyricsCache = {};
    this._splitPreview = null;
    this._splitCategory = null;
    this._splitRetryable = false;
    this._op = null; // the one long action in flight (or its last failure banner)
    this._err = "";
    this._saveMsg = "";
    this._editOpen = false;
    // v6 state
    this._sort = "newest";
    this._qf = { pinned: false, unsorted: false, audio: false };
    this._tracks = {};
    this._tracksInfo = { state: "loading", reason: "" };
    this._blocked = null;
    this._expanded = new Set();
    this._lyricsLoading = new Set();
    this._menu = null; // {id, kind: "copy"|"more", sub: null|"move"}
    this._sheet = "peek";
    this._chipsOpen = false; // narrow layouts clamp the genre chips (#133)
    this._chipsOverflow = false;
    this._focusId = null;
    this._playing = null;
    this._playErr = {};
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
    this._loadData();
  }

  /** True while a long action runs: every other action control is disabled. */
  get _locked() {
    return controlsLocked(this._op);
  }

  get _readOnly() {
    return !!(this._config && this._config.standalone);
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first && this._config) {
      if (this._config.ingress_app) this._loadLivePage();
      else this._render();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._config && !this._clips.length && !this._loadError) {
      if (this._usesLiveSongs()) this._loadLivePage();
      else this._loadData();
    }
    if (!this._keyHandler) this._keyHandler = (e) => this._onKey(e);
    if (typeof window !== "undefined" && window.addEventListener)
      window.addEventListener("keydown", this._keyHandler);
    // A rotation or sidebar resize can re-clamp the chip row (#133).
    if (typeof ResizeObserver !== "undefined" && !this._chipsObserver) {
      this._chipsObserver = new ResizeObserver(() => this._syncChipsMore());
      this._chipsObserver.observe(this);
    }
  }

  disconnectedCallback() {
    if (this._chipsObserver) {
      this._chipsObserver.disconnect();
      this._chipsObserver = null;
    }
    if (this._keyHandler && typeof window !== "undefined" && window.removeEventListener)
      window.removeEventListener("keydown", this._keyHandler);
    if (this._audio) {
      try {
        this._audio.pause();
      } catch (_) {
        /* ignore */
      }
    }
  }

  _ensure() {
    this._genres = ensureGenres(this._genres);
  }

  _usesLiveSongs() {
    return !!(this._config.songs_url || this._config.ingress_app);
  }

  async _songsEndpoint() {
    if (this._config.songs_url) return new URL(this._config.songs_url, location.href);
    if (!this._ingress) {
      this._ingress = (async () => {
        const slug = this._config.ingress_app;
        const [info, { session }] = await Promise.all([
          this._hass.callWS({ type: "supervisor/ingress/info", slug }),
          this._hass.callWS({ type: "supervisor/ingress/session", slug }),
        ]);
        const url = new URL(info.ingress_url, location.origin);
        const root = /^\/api\/hassio_ingress\/[A-Za-z0-9_-]+(?:\/|$)/.exec(url.pathname);
        if (url.origin !== location.origin || !root || typeof session !== "string" || !/^[A-Za-z0-9_-]+$/.test(session)) {
          throw new Error("Invalid Home Assistant ingress configuration");
        }
        const secure = location.protocol === "https:" ? "; Secure" : "";
        document.cookie = "ingress_session=" + session + "; Path=/api/hassio_ingress/; SameSite=Strict" + secure;
        return new URL(root[0].replace(/\/?$/, "/") + "api/songs", location.origin);
      })().catch((error) => {
        this._ingress = null;
        throw error;
      });
    }
    return this._ingress;
  }

  async _fetchSongPage(query, page, { signal, refresh } = {}) {
    const params = new URLSearchParams({
      page, category: query.category, search: query.search, sort: query.sort,
      pinned: query.pinned ? "1" : "0", unsorted: query.unsorted ? "1" : "0",
      audio: query.audio ? "1" : "0",
    });
    if (refresh) params.set("refresh", "1");
    const request = async () => {
      const url = new URL(await this._songsEndpoint());
      url.search = params.toString();
      const response = await fetch(url, { cache: "no-cache", signal });
      return { url, response };
    };
    let { url, response } = await request();
    if (this._config.ingress_app && [401, 403].includes(response.status)) {
      this._ingress = null;
      ({ url, response } = await request());
    }
    if (!response.ok) throw new Error("Catalogue HTTP " + response.status);
    const data = await response.json();
    if (!Array.isArray(data.clips) || !Number.isInteger(data.total) || !Number.isInteger(data.page)) {
      throw new Error("Invalid catalogue response");
    }
    return { data, url };
  }

  async _loadLivePage(refresh = false) {
    if (this._config.ingress_app && !this._hass) return;
    const query = this._songQuery();
    const key = JSON.stringify([query, this._page]);
    if (!refresh && this._liveKey === key) return;
    this._liveKey = key;
    this._liveRequest?.abort();
    const controller = new AbortController();
    this._liveRequest = controller;
    this._livePage = null;
    this._loading = true;
    this._loadError = "";
    this._render();
    try {
      const { data, url } = await this._fetchSongPage(query, this._page, {
        signal: controller.signal, refresh,
      });
      if (controller.signal.aborted) return;
      this._livePage = data;
      this._liveRoot = url.href.replace(/api\/songs(?:\?.*)?$/, "");
      this._page = data.page;
      if (this._focusPage) this._pendingFocus = "#song-page-status";
      this._focusPage = false;
      this._liveKey = JSON.stringify([query, this._page]);
      this._genres = ensureGenres(data.genres);
      this._clips = data.clips;
      this._tracks = data.tracks || {};
      this._tracksInfo = { state: "ok", reason: "" };
    } catch (_) {
      if (controller.signal.aborted) return;
      this._livePage = null;
      this._clips = [];
      this._tracks = {};
      this._loadError = "Could not load the live Trackie catalogue. Check the connection and try again.";
    } finally {
      if (!controller.signal.aborted) {
        this._loading = false;
        this._render();
      }
    }
  }

  async _contextClips(category = this._activeCategory, search = this._search) {
    if (!this._usesLiveSongs()) return filterClips(this._clips, this._genres, category, search);
    const query = { ...this._songQuery(), category, search, pinned: false, unsorted: false, audio: false };
    const first = (await this._fetchSongPage(query, 1)).data;
    const clips = first.clips.slice();
    for (let page = 2; page <= first.pages; page++) {
      clips.push(...(await this._fetchSongPage(query, page)).data.clips);
    }
    return clips;
  }

  async _loadData() {
    if (this._usesLiveSongs()) return this._loadLivePage(!!this._livePage);
    this._loading = true;
    this._loadError = "";
    this._render();
    const cfg = this._config;
    try {
      if (cfg.library_url) {
        const res = await fetch(cfg.library_url, { cache: "no-cache" });
        if (!res.ok) throw new Error("library HTTP " + res.status);
        const body = (await res.json()) || {};
        this._clips = Array.isArray(body.clips) ? body.clips : [];
        this._genres = ensureGenres(body.genres);
        if (body.counts && body.counts.blocked != null) this._blocked = body.counts.blocked;
      } else {
        const [clipsRes, genresRes] = await Promise.all([
          fetch(cfg.clips_url, { cache: "no-cache" }),
          fetch(cfg.genres_url, { cache: "no-cache" }),
        ]);
        if (!clipsRes.ok) throw new Error("clips.json HTTP " + clipsRes.status);
        this._clips = (await clipsRes.json()) || [];
        this._genres = genresRes.ok ? ensureGenres(await genresRes.json()) : ensureGenres(null);
      }
      this._loading = false;
    } catch (e) {
      this._loading = false;
      this._loadError =
        "Could not load " +
        (cfg.library_url || cfg.clips_url) +
        " — " +
        (e.message || String(e)) +
        ". Copy www/suno-genres/ into your HA config www/ folder and add the resource (see README).";
    }
    this._render();
    if (!this._loadError) this._loadTracks(0);
  }

  async _loadTracks(attempt) {
    const url = this._config.tracks_url;
    if (!url) {
      this._tracksInfo = { state: "off", reason: "Play needs suno-dl (tracks_url is not set)." };
      this._render();
      return;
    }
    try {
      const abs = new URL(url, typeof location !== "undefined" ? location.href : undefined);
      if (typeof location !== "undefined" && location.protocol === "https:" && abs.protocol === "http:") {
        // Mixed content would be blocked (and logged) by the browser: degrade quietly.
        this._tracks = {};
        this._tracksInfo = {
          state: "off",
          reason: "Play unavailable: this dashboard is HTTPS but suno-dl at " + abs.host + " is plain HTTP.",
        };
        this._render();
        return;
      }
    } catch (_) {
      /* relative URL in node tests */
    }
    const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), 6000) : null;
    try {
      const res = await fetch(url, Object.assign({ cache: "no-cache" }, ctrl ? { signal: ctrl.signal } : {}));
      if (!res.ok) throw new Error("HTTP " + res.status);
      const body = (await res.json()) || {};
      this._tracks = body.tracks && typeof body.tracks === "object" ? body.tracks : {};
      if (body.counts && body.counts.blocked != null) this._blocked = body.counts.blocked;
      this._tracksInfo = { state: "ok", reason: "", warming: !!body.warming };
      if (body.warming && attempt < 8) setTimeout(() => this._loadTracks(attempt + 1), 4000);
    } catch (_) {
      this._tracks = {};
      this._tracksInfo = {
        state: "off",
        reason: "Play unavailable: suno-dl at " + this._hostOf(url) + " isn't reachable from this browser.",
      };
    } finally {
      if (timer) clearTimeout(timer);
    }
    this._render();
  }

  _hostOf(url) {
    try {
      return new URL(url, typeof location !== "undefined" ? location.href : "http://localhost/").host;
    } catch (_) {
      return String(url);
    }
  }

  /** suno-dl returns root-relative "/media/…"; resolve it against the
   *  suno-dl root that serves tracks_url (absolute :8760 URL, or a relative
   *  "api/tracks" behind the webui / HA ingress). */
  _audioUrl(id) {
    const t = this._tracks[id];
    if (!t || !t.audio) return "";
    if (this._usesLiveSongs()) return this._liveRoot + String(t.audio).replace(/^\//, "");
    try {
      const abs = new URL(this._config.tracks_url, typeof location !== "undefined" ? location.href : undefined).href;
      const root = abs.replace(/api\/tracks(?:[?#].*)?$/, "");
      return root + String(t.audio).replace(/^\//, "");
    } catch (_) {
      return "";
    }
  }

  _filtered() {
    return filterClips(this._clips, this._genres, this._activeCategory, this._search);
  }

  /** What the list shows: category + search, then quick filters, then sort. */
  _visible() {
    const base = this._filtered();
    return sortClips(quickFilter(base, this._genres, this._tracks, this._qf), this._sort, this._tracks);
  }

  _songQuery() {
    return {
      category: this._activeCategory,
      search: this._search,
      sort: this._sort,
      ...this._qf,
    };
  }

  _songPage() {
    const query = JSON.stringify(this._songQuery());
    if (this._pageQuery !== query) {
      this._pageQuery = query;
      this._page = 1;
    }
    if (this._usesLiveSongs()) {
      const key = JSON.stringify([this._songQuery(), this._page]);
      if (this._liveKey !== key && (!this._config.ingress_app || this._hass)) {
        queueMicrotask(() => this._loadLivePage());
      }
      if (this._liveKey !== key || !this._livePage) return { songs: [], total: 0, pages: 1, start: 0 };
      const live = this._livePage;
      return {
        songs: live.clips, total: live.total, pages: live.pages,
        start: (live.page - 1) * SONGS_PER_PAGE,
      };
    }
    const matching = this._visible();
    const total = matching.length;
    const pages = Math.max(1, Math.ceil(total / SONGS_PER_PAGE));
    this._page = Math.min(this._page, pages);
    const start = (this._page - 1) * SONGS_PER_PAGE;
    return { songs: matching.slice(start, start + SONGS_PER_PAGE), total, pages, start };
  }

  _selectCategory(name, focus) {
    const prev = this._activeCategory;
    this._activeCategory = name || "All";
    if (focus) this._pendingFocus = focus;
    else if (this._activeCategory === "All" && prev && prev !== "All") this._pendingFocus = '[data-cat="' + String(prev).replace(/["\\]/g, "\\$&") + '"]';
    this._err = "";
    this._saveMsg = "";
    this._menu = null;
    this._sheet = "peek";
    this._render();
  }

  /* ───────────── playback ───────────── */

  _ensureAudio() {
    if (this._audio || typeof Audio === "undefined") return this._audio;
    const a = new Audio();
    a.preload = "none";
    a.addEventListener("timeupdate", () => this._updateProgress());
    a.addEventListener("play", () => this._render());
    a.addEventListener("pause", () => this._render());
    a.addEventListener("ended", () => {
      this._playing = null;
      this._render();
    });
    a.addEventListener("error", () => {
      if (!this._playing) return;
      this._playErr[this._playing] = "This file couldn't be loaded from suno-dl.";
      this._playing = null;
      this._render();
    });
    this._audio = a;
    return a;
  }

  _isPlaying(id) {
    return this._playing === id && this._audio && !this._audio.paused;
  }

  _togglePlay(id) {
    const url = this._audioUrl(id);
    if (!url || this._playErr[id]) return;
    const a = this._ensureAudio();
    if (!a) return;
    if (this._playing === id) {
      if (a.paused) a.play().catch(() => {});
      else a.pause();
      return;
    }
    this._playing = id;
    a.src = url;
    const p = a.play();
    if (p && p.catch)
      p.catch((e) => {
        if (e && e.name === "AbortError") return;
        this._playErr[id] = "Playback was blocked by the browser (" + ((e && e.name) || "error") + ").";
        if (this._playing === id) this._playing = null;
        this._render();
      });
    this._render();
  }

  _updateProgress() {
    if (!this.shadowRoot || !this._audio) return;
    const el = this.shadowRoot.querySelector("[data-prog]");
    if (el && this._audio.duration) el.style.width = (100 * this._audio.currentTime) / this._audio.duration + "%";
  }

  /* ───────────── cards: expand, copy, pin, move ───────────── */

  async _toggleExpand(id) {
    if (this._expanded.has(id)) {
      this._expanded.delete(id);
      this._render();
      return;
    }
    this._expanded.add(id);
    const clip = (this._clips || []).find((c) => c.id === id);
    if (!this._lyricsCache[id] && !(clip && clip.lyrics)) {
      this._lyricsLoading.add(id);
      this._render();
      await this._fetchLyrics([id]);
      this._lyricsLoading.delete(id);
    }
    this._render();
  }

  _lyricsFor(clip) {
    return this._lyricsCache[clip.id] || clip.lyrics || "";
  }

  async _copySong(clip, kind, button) {
    const menuToRestore = this._menu;
    const k = kind || "all";
    if (k === "all" || k === "lyrics") await this._fetchLyrics([clip.id]);
    const lyrics = this._lyricsFor(clip);
    let text;
    if (k === "lyrics") text = lyrics || "(no lyrics saved for this song)";
    else if (k === "style") text = clip.style || "";
    else if (k === "title") text = clip.title || "";
    else {
      const lines = ["Title: " + (clip.title || ""), "Style: " + (clip.style || "")];
      if (lyrics) lines.push("Lyrics:\n" + lyrics);
      text = lines.join("\n");
    }
    this._menu = null;
    await this._copy(text, button || null);
    if (menuToRestore) this._focusMenuTrigger(menuToRestore);
  }

  async _togglePin(songId) {
    if (this._readOnly) return;
    const menuToRestore = this._menu;
    const g = JSON.parse(JSON.stringify(this._genres));
    g.pinned = g.pinned || [];
    const i = g.pinned.indexOf(songId);
    if (i >= 0) g.pinned.splice(i, 1);
    else g.pinned.push(songId);
    this._menu = null;
    await this._save(recount(g), i >= 0 ? "Unpinned" : "Pinned");
    if (menuToRestore) this._focusMenuTrigger(menuToRestore);
  }

  async _clearCategory(catName) {
    if (this._readOnly) return;
    const g = JSON.parse(JSON.stringify(this._genres));
    const rec = this._categoryRecord(catName);
    const childNames = new Set(rec && rec.cat && rec.cat.children ? rec.cat.children.map((ch) => ch.name) : []);
    if (
      !g.categories.some(
        (c) => c.name === "Uncategorised" || (c.children || []).some((ch) => ch.name === "Uncategorised")
      )
    )
      g.categories.push({ name: "Uncategorised", description: "", count: 0 });
    g.pinned = g.pinned || [];
    for (const [id, leaf] of Object.entries(g.assignments || {})) {
      if (leaf === catName || childNames.has(leaf)) {
        g.assignments[id] = "Uncategorised";
        if (!g.pinned.includes(id)) g.pinned.push(id);
      }
    }
    this._activeCategory = "All";
    await this._save(recount(g), "Cleared " + catName);
  }

  _categoryRecord(name) {
    for (const c of this._genres.categories || []) {
      if (c.name === name) return { cat: c, parent: null };
      if (c.children) for (const ch of c.children) if (ch.name === name) return { cat: ch, parent: c };
    }
    return null;
  }

  _paneStats(name) {
    const live = this._livePage?.category_stats;
    const total = this._livePage?.counts.archive || (this._clips || []).length || 1;
    const songs = clipsInCategory(this._clips, this._genres, name);
    const tracks = live ? live.tracks : songs.length;
    const pinnedSet = new Set(this._genres.pinned || []);
    const rec = this._categoryRecord(name);
    const children = (rec && rec.cat && rec.cat.children) || [];
    const isParent = children.length > 0;
    const parentName = rec && rec.parent ? rec.parent.name : null;
    let meta = "leaf genre";
    if (parentName) meta = "subtier of " + parentName;
    else if (isParent) meta = "parent · " + children.length + " subtier" + (children.length === 1 ? "" : "s");
    return {
      name,
      songs,
      tracks,
      pinned: live ? live.pinned : songs.filter((c) => pinnedSet.has(c.id)).length,
      mix: live?.mix,
      pct: Math.round((100 * tracks) / total),
      children,
      meta,
      splittable: !isParent && !parentName && tracks >= MIN_SPLIT_SONGS,
      isParent,
      parentName,
    };
  }

  /* ───────────── keyboard ───────────── */

  _onKey(e) {
    if (!this.isConnected || !this.shadowRoot) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (!this.getClientRects || !this.getClientRects().length) return; // card hidden
    const path = e.composedPath ? e.composedPath() : [];
    // Only handle shortcuts from this card. Global window listeners must not
    // steal Space/Enter from dashboard buttons or neighboring HA cards.
    if (!path.includes(this)) return;
    const within = path.slice(0, path.indexOf(this));
    const t = path[0] || e.target;
    const tag = t && t.tagName ? String(t.tagName).toLowerCase() : "";
    const typing = tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable);
    const interactive = within.some((node) => {
      const name = node && node.tagName ? String(node.tagName).toLowerCase() : "";
      const role = node && node.getAttribute ? node.getAttribute("role") : null;
      return ["button", "a", "summary", "option", "input", "textarea", "select"].includes(name)
        || (node && node.isContentEditable) || role === "button" || role === "link";
    });
    if (e.key === "Escape") {
      const closingMenu = this._menu;
      if (typing && t.blur) t.blur();
      if (this._menu) this._menu = null;
      else if (this._activeCategory !== "All" && this._sheet === "full" && this._narrow()) this._sheet = "peek";
      else if (this._activeCategory !== "All") {
        this._pendingFocus = '[data-cat="' + String(this._activeCategory).replace(/["\\]/g, "\\$&") + '"]';
        this._activeCategory = "All";
      } else return;
      e.preventDefault();
      this._render();
      if (closingMenu) this._focusMenuTrigger(closingMenu);
      return;
    }
    if (typing || interactive) return;
    const ids = this._songPage().songs.map((c) => c.id);
    if (e.key === "/") {
      e.preventDefault();
      const q = this.shadowRoot.querySelector("#q");
      if (q) q.focus();
    } else if (e.key === "j" || e.key === "k") {
      if (!ids.length) return;
      e.preventDefault();
      let i = ids.indexOf(this._focusId);
      i = e.key === "j" ? Math.min(ids.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
      this._focusId = ids[i];
      this._render();
      const el = this.shadowRoot.querySelector('[data-card="' + CSS_ESCAPE(this._focusId) + '"]');
      if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
    } else if (e.key === " " && this._focusId) {
      e.preventDefault();
      this._togglePlay(this._focusId);
    } else if ((e.key === "Enter" || e.key === "o") && this._focusId) {
      e.preventDefault();
      this._toggleExpand(this._focusId);
    }
  }

  _narrow() {
    const w = this.shadowRoot && this.shadowRoot.querySelector(".wrap");
    return !!(w && w.getBoundingClientRect && w.getBoundingClientRect().width <= 700);
  }

  /**
   * Run one long action with a visible working state (spinner + one status
   * line + progress), disabling every other action until it ends. A failure
   * leaves an error banner (cause class, what completed, request id, time,
   * Retry / Copy details) instead of a generic toast.
   */
  async _runOp(spec, fn) {
    if (this._locked) return false;
    const op = createOp(spec);
    this._op = op;
    this._err = "";
    this._saveMsg = "";
    this._render();
    let ok = false;
    try {
      await fn(op);
      opCheck(op);
      ok = true;
    } catch (e) {
      failOp(op, e);
      this._reportFailure(op);
    }
    if (ok) this._op = null;
    this._render();
    return ok;
  }

  _reportFailure(op) {
    const e = op.error;
    if (!e || e.code === "cancelled") return;
    if (typeof console !== "undefined" && console.warn) console.warn("[suno-genres " + e.request_id + "] " + op.kind + " failed: " + e.cause + " - " + e.detail);
    const url = this._config && this._config.client_error_url;
    if (!url || typeof fetch !== "function") return;
    try {
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: e.request_id, kind: op.kind, code: e.code, cause: e.cause, detail: e.detail }),
      }).catch(() => {});
    } catch (_) {
      /* reporting is best-effort */
    }
  }

  async _save(updated, msg) {
    if (this._readOnly) return;
    return this._runOp(
      { kind: "save", title: "Saving genre changes", step: "writing", unit: "changes", noChangeNote: "The change may not have been saved.", retry: () => this._save(updated, msg) },
      async () => {
        let ok = false;
        if (this._hass) {
          try {
            await this._hass.callService("rest_command", "suno_genres_apply", {
              genres_json: JSON.stringify(updated),
            });
            ok = true;
            this._saveMsg = msg || "Saved via rest_command";
          } catch (_) {
            ok = false;
          }
        }
        if (!ok) {
          if (!this._config.apply_url) throw new Error("no remote configured (apply_url)");
          const res = await fetch(this._config.apply_url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updated),
          });
          if (!res.ok) throw apiErrorFrom(res.status, await res.json().catch(() => null), "Apply");
          const body = await res.json().catch(() => ({}));
          this._saveMsg = body.message || msg || "Saved";
        }
        this._genres = ensureGenres(updated);
        await this._loadData();
      }
    );
  }

  async _undo() {
    return this._runOp(
      { kind: "undo", title: "Undoing the last change", step: "restoring", unit: "changes", noChangeNote: "The last change may still be applied.", retry: () => this._undo() },
      async () => {
        let ok = false;
        if (this._hass) {
          try {
            await this._hass.callService("rest_command", "suno_genres_undo", {});
            ok = true;
            this._saveMsg = "Undo via rest_command";
          } catch (_) {
            ok = false;
          }
        }
        if (!ok) {
          if (!this._config.undo_url) throw new Error("no remote configured (undo_url)");
          const res = await fetch(this._config.undo_url, { method: "POST" });
          if (!res.ok) throw apiErrorFrom(res.status, await res.json().catch(() => null), "Undo");
          this._saveMsg = "Undo via API";
        }
        await this._loadData();
      }
    );
  }

  async _reclassify() {
    if (
      !confirm(
        "Reclassify all unpinned songs against the current tree? Hand-moved (pinned) songs are kept."
      )
    )
      return;
    return this._runOp(
      { kind: "reclassify", title: "Reclassifying unpinned songs", step: "waiting for the classifier", unit: "songs", noChangeNote: "Songs may be only partly reclassified.", retry: () => this._reclassify() },
      async () => {
        let viaRest = false;
        if (this._hass) {
          try {
            await this._hass.callService("rest_command", "suno_genres_reclassify", {});
            viaRest = true;
            await new Promise((r) => setTimeout(r, 2000));
            await this._loadData();
            this._saveMsg = "Reclassify via rest_command (pinned preserved)";
          } catch (_) {
            viaRest = false;
          }
        }
        if (!viaRest) {
          if (!this._config.reclassify_url) throw new Error("no remote configured (reclassify_url)");
          const res = await fetch(this._config.reclassify_url, { method: "POST" });
          if (!res.ok) throw apiErrorFrom(res.status, await res.json().catch(() => null), "Reclassify");
          const data = await res.json();
          if (data.genres) this._genres = ensureGenres(data.genres);
          else await this._loadData();
          this._saveMsg = data.message || "Reclassified";
        }
      }
    );
  }

  async _addCategory() {
    const name = (prompt("New category name:") || "").trim();
    if (!name) return;
    this._ensure();
    const g = JSON.parse(JSON.stringify(this._genres));
    if (leafNames(g).includes(name) || g.categories.some((c) => c.name === name)) {
      this._err = "A category named " + name + " already exists.";
      this._render();
      return;
    }
    g.categories.push({ name, description: "", count: 0 });
    await this._save(g, "Added " + name);
  }

  async _renameCategory(oldName) {
    const name = (prompt('Rename "' + oldName + '" to:', oldName) || "").trim();
    if (!name || name === oldName) return;
    const g = JSON.parse(JSON.stringify(this._genres));
    for (const c of g.categories) {
      if (c.name === oldName) c.name = name;
      if (c.children)
        for (const ch of c.children) if (ch.name === oldName) ch.name = name;
    }
    for (const id of Object.keys(g.assignments || {}))
      if (g.assignments[id] === oldName) g.assignments[id] = name;
    await this._save(recount(g), "Renamed to " + name);
  }

  async _deleteCategory(catName) {
    if (!confirm('Delete "' + catName + '"? Songs go to Uncategorised and are pinned.')) return;
    const g = JSON.parse(JSON.stringify(this._genres));
    const orig = (this._genres.categories || []).find((c) => c.name === catName);
    const childNames = new Set(orig && orig.children ? orig.children.map((ch) => ch.name) : []);
    g.categories = (g.categories || [])
      .map((c) => {
        if (c.name === catName) return null;
        if (c.children) {
          c.children = c.children.filter((ch) => ch.name !== catName);
          if (!c.children.length) delete c.children;
        }
        return c;
      })
      .filter(Boolean);
    if (
      !g.categories.some(
        (c) => c.name === "Uncategorised" || (c.children || []).some((ch) => ch.name === "Uncategorised")
      )
    )
      g.categories.push({ name: "Uncategorised", description: "", count: 0 });
    g.pinned = g.pinned || [];
    for (const [id, leaf] of Object.entries(g.assignments || {})) {
      if (leaf === catName || childNames.has(leaf)) {
        g.assignments[id] = "Uncategorised";
        if (!g.pinned.includes(id)) g.pinned.push(id);
      }
    }
    await this._save(recount(g), "Deleted " + catName);
  }

  async _moveSong(songId, newCat) {
    if (!newCat || this._readOnly) return;
    const g = JSON.parse(JSON.stringify(this._genres));
    g.assignments[songId] = newCat;
    g.placement[songId] = "manual";
    g.pinned = g.pinned || [];
    if (!g.pinned.includes(songId)) g.pinned.push(songId);
    await this._save(recount(g), "Moved (pinned)");
  }

  async _copy(text, button) {
    const done = () => {
      const old = button ? button.textContent : "";
      if (button) {
        button.textContent = "Copied";
        setTimeout(() => {
          button.textContent = old;
        }, 1200);
      }
      this._saveMsg = "Copied to clipboard.";
      this._render();
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        done();
        return;
      }
    } catch (_) {
      /* fall through to the legacy path */
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      done();
    } catch (e) {
      this._err = "Copy failed: " + (e.message || String(e));
      this._render();
    }
    document.body.removeChild(ta);
  }

  async _fetchLyrics(ids) {
    const need = ids.filter((id) => !this._lyricsCache[id]).slice(0, 40);
    if (!need.length) return;
    const q = need.join(",");
    let data = null;
    try {
      const res = await fetch(this._config.lyrics_url + "?ids=" + encodeURIComponent(q));
      if (res.ok) data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!data && this._hass) {
      try {
        await this._hass.callService("rest_command", "suno_genres_lyrics_warm", { ids: q });
        const res2 = await fetch(this._config.lyrics_url + "?ids=" + encodeURIComponent(q));
        if (res2.ok) data = await res2.json();
      } catch (_) {
        data = null;
      }
    }
    if (data && data.lyrics) for (const [id, t] of Object.entries(data.lyrics)) this._lyricsCache[id] = t;
  }

  async _contextForAI(clips) {
    await this._fetchLyrics(clips.map((c) => c.id));
    return packAiContext(clips, this._lyricsCache);
  }

  async _sendAI() {
    if (!this._hass) {
      this._aiError = "Home Assistant is not connected yet — reload the dashboard and try again.";
      this._render();
      return;
    }
    const total = this._livePage?.filter_counts.total ?? this._filtered().length;
    if (!total) {
      this._aiError = "No songs in the current filter, so there is nothing to send.";
      this._render();
      return;
    }
    this._aiLoading = true;
    this._aiError = "";
    this._aiResponse = "";
    this._ctxForFallback = null;
    await this._runOp(
      {
        kind: "ask-ai",
        title: "Asking the AI about " + total + " song" + (total === 1 ? "" : "s"),
        step: "packing context",
        unit: "songs",
        cancellable: true,
        noChangeNote: "No reply was received.",
        retry: () => this._sendAI(),
      },
      async (op) => {
        const ctx = await this._contextForAI(await this._contextClips());
        opCheck(op);
        this._ctxForFallback = ctx;
        this._lastCtxNote = summariseContext(ctx);
        const question =
          (this._prompt || "").trim() ||
          "Write the next song in this series (title, style, lyrics)";
        const full =
          question +
          "\n\nThis is an archive of the user's own generated tracks. Continue the series.\n\nContext: " +
          ctx.used.length +
          " songs.\n\n" +
          ctx.text;
        opUpdate(op, { step: "waiting for the reply", total: ctx.used.length, done: 0 });
        const speech = extractSpeech(await this._agent(full, op));
        if (isRefusalText(speech)) {
          this._aiResponse = speech;
          this._aiError =
            "The agent refused to continue the songs (" +
            (this._config.agent_id || DEFAULT_AGENT_ID) +
            ' said: "' +
            replySnippet(speech, 80) +
            '"). Point agent_id at a model that allows songwriting, or paste the packed context into any chat.';
        } else {
          this._aiResponse = speech || "(empty reply)";
        }
      }
    );
    this._aiLoading = false;
    this._render();
  }

  async _agent(text, op) {
    const call = this._hass.connection.sendMessagePromise({
      type: "call_service",
      domain: "conversation",
      service: "process",
      return_response: true,
      service_data: { agent_id: this._config.agent_id || DEFAULT_AGENT_ID, text },
    });
    const secs = Number(this._config.agent_timeout_s) > 0 ? Number(this._config.agent_timeout_s) : AGENT_TIMEOUT_MS / 1000;
    const result = await withTimeout(call, secs * 1000, "The AI service (" + (this._config.agent_id || DEFAULT_AGENT_ID) + ")", op);
    assertAgentOk(result);
    if (!extractSpeechStrict(result).trim()) throw new ReplyParseError("Agent", "", "the response carried no speech");
    return result;
  }

  /**
   * Split a category into 2-4 subtiers. One retry with a stricter
   * JSON-only prompt when the first reply is prose or a refusal.
   */
  async _splitWithAI(catName, isRetry) {
    if (!this._hass) {
      this._err = "Home Assistant is not connected yet — reload the dashboard and try again.";
      this._render();
      return;
    }
    let songs;
    try {
      songs = await this._contextClips(catName, "");
    } catch (_) {
      this._err = "Could not load the songs for this genre.";
      return this._render();
    }
    if (songs.length < MIN_SPLIT_SONGS) {
      this._err =
        "Split needs at least " +
        MIN_SPLIT_SONGS +
        " songs in " +
        catName +
        " (found " +
        songs.length +
        ").";
      this._render();
      return;
    }
    this._err = "";
    this._splitPreview = null;
    this._liveNote = "";
    this._splitRetryable = false;
    this._splitCategory = catName;
    const total = songs.length;
    await this._runOp(
      {
        kind: "split",
        title: 'Splitting "' + catName + '"',
        step: "asking the AI for subcategories",
        unit: "tracks",
        total,
        cancellable: true,
        doneVerb: "analysed",
        noChangeNote: "Nothing was changed: the split was not applied.",
        retry: (err) => this._splitWithAI(catName, isRetry || !!(err && err.code === "ai_invalid_reply" || err && err.code === "ai_empty_reply")),
      },
      async (op) => {
        const compact = songs.map((c) => ({
          id: c.id,
          title: (c.title || "").slice(0, 50),
          style: (c.style || "").slice(0, 120),
        }));
        const step = Math.max(1, Math.floor(compact.length / 40));
        const sample = compact.filter((_, i) => i % step === 0).slice(0, 45);
        const taxPrompt =
          'Category "' +
          catName +
          '" has ' +
          songs.length +
          " songs. Propose 2-4 subcategories from the styles.\n" +
          'JSON ONLY: {"subcategories":[{"name":"...","description":"..."}]}\n\nSample:\n' +
          JSON.stringify(sample);

        const prompt = isRetry ? strictJsonPrompt(taxPrompt) : taxPrompt;
        const tax = parseJsonReply(extractSpeech(await this._agent(prompt, op)), "Split");
        opCheck(op);
        const subcats = normalizeSubcategories(tax);
        if (subcats.length < 2)
          throw new Error(
            "Split: the agent proposed " +
              subcats.length +
              " subcategor" +
              (subcats.length === 1 ? "y" : "ies") +
              ", but 2-4 are required."
          );
        const names = subcats.map((s) => s.name);
        opUpdate(op, {
          title: 'Splitting "' + catName + '" into ' + names.join(" + "),
          step: "analysing",
          showCounts: true,
          done: 0,
        });
        this._render();
        const assignments = {};
        for (let i = 0; i < compact.length; i += MAX_SUBTIER_ASSIGNMENT_CHUNK) {
          opCheck(op);
          const batch = compact.slice(i, i + MAX_SUBTIER_ASSIGNMENT_CHUNK);
          const assignPrompt =
            "Assign each to one of: " +
            names.join(", ") +
            '.\nJSON ONLY: {"assignments":{"uuid":"Name"}}\nSongs:\n' +
            JSON.stringify(batch);
          const res = await this._agent(isRetry ? strictJsonPrompt(assignPrompt) : assignPrompt, op);
          const spoken = extractSpeech(res);
          mergeAssignments(parseJsonReply(spoken, "Split"), names, assignments);
          if (!batch.some((s) => assignments[s.id]))
            throw new ReplyParseError("Split", spoken, "no song in the reply matched a subcategory");
          // Songs the AI left out of its reply are NOT guessed into a subcategory
          // (they used to fall into the last one); they stay where they are.
          opUpdate(op, { done: Math.min(compact.length, i + batch.length) });
          this._render();
        }
        const counts = countAssignments(assignments, names);
        const unclassified = songs.filter((c) => !assignments[c.id]).length;
        this._splitPreview = {
          parent: catName,
          unclassified,
          subcategories: subcats.map((s) => ({ name: s.name, description: s.description, count: counts[s.name] || 0 })),
          assignments,
        };
        this._splitRetryable = false;
      }
    );
    if (this._op && this._op.state === "failed" && this._op.error) this._splitPreview = null;
    if (this._splitPreview) {
      const n = this._splitPreview.unclassified;
      this._liveNote =
        "Split preview ready for " + catName + ": " + this._splitPreview.subcategories.length + " subcategories" +
        (n ? ", " + n + " song(s) not classified" : "") + ". Review it, then Apply or Cancel.";
      if (this._narrow()) this._sheet = "full"; // the preview lives in the expanded sheet
      this._render();
      const panel = this.shadowRoot.querySelector(".pane .panel");
      if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
    }
  }

  async _retrySplit() {
    const cat = this._splitCategory;
    if (!cat) return;
    await this._splitWithAI(cat, true);
  }

  async _applySplit() {
    const preview = this._splitPreview;
    if (!preview) return;
    try {
      const g = applySplitPreview(this._genres, preview);
      this._splitPreview = null;
      this._splitRetryable = false;
      this._activeCategory = preview.parent;
      await this._save(g, "Split applied to " + preview.parent);
    } catch (e) {
      this._err = "Split apply failed: " + (e.message || String(e));
      this._render();
    }
  }


  _esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _playState(c) {
    const info = this._tracksInfo || {};
    const t = this._tracks[c.id];
    if (info.state === "loading") return { off: true, reason: "Checking suno-dl for audio…", noAudio: false };
    if (info.state !== "ok") return { off: true, reason: info.reason || "Play unavailable.", noAudio: false };
    if (!t || !t.audio)
      return { off: true, reason: "No local audio file in the suno-dl archive for this song.", noAudio: true };
    if (this._playErr[c.id]) return { off: true, reason: this._playErr[c.id], noAudio: false };
    return { off: false, reason: this._isPlaying(c.id) ? "Pause" : "Play", noAudio: false };
  }

  _menuHtml(c) {
    const m = this._menu;
    if (!m || m.id !== c.id) return "";
    const id = this._esc(c.id);
    // Exactly one action group is open in this card's shadow root. Native
    // buttons retain their Tab/Shift+Tab behavior; this is not an ARIA menu.
    const label = (m.kind === "copy" ? "Copy options" : m.sub === "move" ? "Move to genre" : "More actions")
      + " for " + (c.title || c.id);
    const group = '<div class="menu" id="track-action-group" role="group" aria-label="' + this._esc(label) + '">';
    if (m.kind === "copy") {
      return (
        group +
        '<button type="button" data-copy-kind="all" data-song="' + id + '">Copy title + style + lyrics <span>default</span></button>' +
        '<button type="button" data-copy-kind="lyrics" data-song="' + id + '">Copy lyrics only</button>' +
        '<button type="button" data-copy-kind="style" data-song="' + id + '">Copy style only</button>' +
        '<button type="button" data-copy-kind="title" data-song="' + id + '">Copy title only</button>' +
        "</div>"
      );
    }
    const ro = this._readOnly;
    const pinned = (this._genres.pinned || []).includes(c.id);
    if (m.sub === "move" && !ro) {
      const cur = (this._genres.assignments || {})[c.id] || "";
      const leaves = leafNames(this._genres);
      return (
        group + '<button type="button" data-menu-back="1">← Move to genre</button><hr><div class="mlist">' +
        leaves
          .map(
            (n) =>
              '<button type="button" data-move-to="' + this._esc(n) + '" data-song="' + id + '"' +
              (n === cur ? ' class="cur" disabled' : "") + ">" + this._esc(n) +
              (n === cur ? " <span>current</span>" : "") + "</button>"
          )
          .join("") +
        "</div></div>"
      );
    }
    return (
      group +
      '<button type="button" data-menu-move="' + id + '"' + (ro || this._locked ? " disabled" : "") + ">Move to genre… <span>▸</span></button>" +
      '<button type="button" data-pin="' + id + '"' + (ro || this._locked ? " disabled" : "") + ">" + (pinned ? "Unpin" : "Pin here") + "</button>" +
      (ro ? '<div class="mreason">Read-only here: move and pin songs on the Home Assistant card.</div>' : "") +
      "<hr>" +
      '<button type="button" data-copy-kind="all" data-song="' + id + '">Copy title + style + lyrics</button>' +
      '<button type="button" data-copy-kind="lyrics" data-song="' + id + '">Copy lyrics only</button>' +
      "<hr>" +
      '<button type="button" data-open="' + id + '">Open on Suno <span>↗</span></button>' +
      "</div>"
    );
  }

  _cardHtml(c, pinned) {
    const id = this._esc(c.id);
    const leaf = (this._genres.assignments || {})[c.id] || "Uncategorised";
    const parents = parentMap(this._genres);
    const leafShort = parents[leaf] ? shortChildName(parents[leaf], leaf) : leaf;
    const t = this._tracks[c.id] || {};
    const ps = this._playState(c);
    const playing = this._isPlaying(c.id);
    const expanded = this._expanded.has(c.id);
    const bpm = parseBpm(c.style);
    const dur = fmtDuration(t.duration);
    const date = fmtDate(t.created);
    const dateTitle =
      t.created_source === "suno" ? "Created on Suno" : t.created_source ? "Archive file date (no Suno date saved)" : "";
    const metaBits = [];
    if (dur) metaBits.push("<span>" + dur + "</span>");
    if (date) metaBits.push('<span title="' + this._esc(dateTitle) + '">' + this._esc(date) + "</span>");
    if (bpm) metaBits.push("<span>" + bpm + " BPM</span>");
    const menuOpen = this._menu && this._menu.id === c.id ? this._menu.kind : "";
    let full = "";
    if (expanded) {
      const lyr = this._lyricsFor(c);
      const lyrHtml = this._lyricsLoading.has(c.id)
        ? '<p class="note">Loading lyrics…</p>'
        : lyr
        ? "<pre>" + this._esc(lyr) + "</pre>"
        : '<p class="note">No lyrics saved for this song.</p>';
      full =
        '<div class="full"><div><h4>Style</h4><p>' + this._esc(c.style || "") + "</p></div><div><h4>Lyrics</h4>" + lyrHtml + "</div></div>";
    }
    return (
      '<div class="song' + (expanded ? " exp" : "") + (this._focusId === c.id ? " foc" : "") + (playing ? " playing" : "") +
      '" data-card="' + id + '">' +
      '<button type="button" class="play' + (ps.off ? " off" : "") + (playing ? " on" : "") + '" data-play="' + id +
      '" title="' + this._esc(ps.reason) + '" aria-label="' + this._esc(ps.reason) + '"' + (ps.off ? " disabled" : "") + ">" +
      (playing ? PAUSE_SVG : PLAY_SVG) + "</button>" +
      '<div class="tl"><a class="title" title="' + this._esc(c.title || c.id) + '" href="https://suno.com/song/' + id + '" target="_blank" rel="noopener">' +
      this._esc(c.title || c.id) + "</a>" +
      '<span class="badge cat">' + this._esc(leafShort) + "</span>" +
      (c.model ? '<span class="badge">' + this._esc(c.model) + "</span>" : "") +
      (pinned.has(c.id) ? '<span class="badge pin">pinned</span>' : "") +
      (ps.noAudio ? '<span class="badge warn">no local audio</span>' : "") +
      "</div>" +
      '<div class="acts"><span class="splitb' + (menuOpen === "copy" ? " on" : "") + '">' +
      '<button type="button" data-copy-song="' + id + '" title="Copy title, style and lyrics">Copy</button>' +
      '<button type="button" data-menu-copy="' + id + '" aria-label="Copy options" aria-expanded="' +
      (menuOpen === "copy" ? "true" : "false") + '"' +
      (menuOpen === "copy" ? ' aria-controls="track-action-group"' : "") + '>▾</button></span>' +
      '<button type="button" class="more' + (menuOpen === "more" ? " on" : "") + '" data-menu-more="' + id +
      '" aria-label="More actions" aria-expanded="' + (menuOpen === "more" ? "true" : "false") + '"' +
      (menuOpen === "more" ? ' aria-controls="track-action-group"' : "") + '>⋯</button></div>' +
      '<div class="meta">' + metaBits.join('<span class="dot">·</span>') +
      '<span class="badge cat mob">' + this._esc(leafShort) + "</span>" +
      (ps.noAudio ? '<span class="badge warn mob">no local audio</span>' : "") +
      '<button type="button" class="lnk" data-expand="' + id + '" aria-expanded="' + !!expanded + '">' + (expanded ? "▴ collapse" : "▾ expand") + "</button></div>" +
      (expanded ? "" : '<div class="style">' + this._esc(c.style || "") + "</div>") +
      full +
      (playing || this._playing === c.id ? '<div class="prog"><i data-prog></i></div>' : "") +
      this._menuHtml(c) +
      "</div>"
    );
  }

  _askHtml(count) {
    const noAgent = !this._hass;
    return (
      '<div class="ask" id="ask-block"><div class="sec">Ask AI · ' + count + " song" + (count === 1 ? "" : "s") + " in context</div>" +
      '<div class="note">' + this._esc(this._lastCtxNote || "Packs title, style and lyrics for the current filter.") + "</div>" +
      '<textarea id="prompt" rows="2">' + this._esc(this._prompt) + "</textarea>" +
      '<div class="row"><button type="button" class="act" id="send"' + (this._aiLoading || noAgent || this._locked ? " disabled" : "") + ">" +
      (this._aiLoading ? "Sending…" : "Send") + "</button>" +
      '<button type="button" class="act secondary" id="copy-context">Copy context</button>' +
      '<button type="button" class="act secondary" id="copy"' + (this._aiResponse ? "" : " disabled") + ">Copy reply</button></div>" +
      (noAgent
        ? '<div class="reason">' +
          (this._readOnly
            ? "Ask AI runs on the Home Assistant card (it needs HA's conversation agent). Copy context and paste it into any chat."
            : "Home Assistant isn't connected yet, so Send is unavailable. Copy context still works.") +
          "</div>"
        : "") +
      (this._aiError ? '<div class="err">' + this._esc(this._aiError) + "</div>" : "") +
      (this._aiResponse ? '<pre class="out">' + this._esc(this._aiResponse) + "</pre>" : "") +
      "</div>"
    );
  }

  _paneHtml(stats) {
    const ro = this._readOnly;
    const mix = stats.mix || classifierMix(stats.songs, this._genres);
    const shares = childShares(this._genres, stats.name);
    const updated = fmtStamp(this._genres.generated_at);
    const reason = splitReason(stats, { readOnly: ro, noAgent: !this._hass });
    const bar = shares.length
      ? '<div class="bar">' +
        shares.map((s) => '<i style="width:' + s.pct + "%;background:" + s.color + '" title="' + this._esc(s.name + " · " + s.count) + '"></i>').join("") +
        "</div>"
      : "";
    const kids = shares.length
      ? '<div class="kids">' +
        shares
          .map(
            (s) =>
              '<button type="button" class="kid' + (s.current ? " cur" : "") + '" data-cat="' + this._esc(s.name) + '"><i style="background:' +
              s.color + '"></i>' + this._esc(s.name) + '<span class="c">' + s.count + " · " + s.pct + "%</span></button>"
          )
          .join("") +
        "</div>"
      : '<div class="note">No subtiers yet.</div>';
    const mixBadges =
      '<span class="badge cat">system1 ' + mix.system1 + "</span>" +
      '<span class="badge">LLM ' + mix.llm + "</span>" +
      '<span class="badge">abstained ' + mix.abstained + "</span>" +
      (mix.manual ? '<span class="badge">manual ' + mix.manual + "</span>" : "") +
      '<span class="badge">pinned ' + mix.pinned + "</span>";
    let preview = "";
    if (this._splitPreview && this._splitPreview.parent === stats.name) {
      const p = this._splitPreview;
      preview =
        '<div class="panel"><h3>Split preview</h3><ul class="subcats">' +
        p.subcategories.map((s) => "<li>" + this._esc(s.name) + ' <span class="n">' + (s.count || 0) + " song(s)</span></li>").join("") +
        '</ul>' +
        (p.unclassified ? '<p class="note" id="split-unclassified">' + p.unclassified + " song(s) were not classified by the AI and will stay in " + this._esc(p.parent) + ".</p>" : "") +
        '<div class="row"><button type="button" class="act" id="apply-split"' + (this._locked ? " disabled" : "") + ">Apply</button>" +
        '<button type="button" class="act secondary" id="cancel-split"' + (this._locked ? " disabled" : "") + ">Cancel</button></div></div>";
    }
    const retry = this._splitRetryable
      ? '<div class="row"><button type="button" class="act" id="retry-split">Retry with JSON-only prompt</button></div>'
      : "";
    const splitOk = !ro && stats.splittable && !!this._hass && !this._locked;
    const actions =
      '<div><div class="sec">Actions</div><div class="row">' +
      '<button type="button" class="act' + (splitOk ? "" : " secondary") + '" id="pane-split"' + (splitOk ? "" : " disabled") + ">Split</button>" +
      '<button type="button" class="act secondary" id="pane-edit"' + (ro || this._locked ? " disabled" : "") + ">Edit</button>" +
      '<button type="button" class="act secondary" id="pane-clear"' + (ro || this._locked ? " disabled" : "") + ">Clear</button>" +
      (this._editOpen && !ro
        ? '<button type="button" class="act secondary" id="pane-rename"' + (this._locked ? " disabled" : "") + '>Rename</button><button type="button" class="mini danger" id="pane-delete"' + (this._locked ? " disabled" : "") + '>Delete</button>'
        : "") +
      "</div>" + (reason ? '<div class="reason" style="margin-top:8px">' + this._esc(reason) + "</div>" : "") + "</div>";
    return (
      '<aside class="pane open ' + (this._sheet === "full" ? "full" : "peek") + '" id="genre-pane" tabindex="-1" aria-label="' + this._esc("Genre details: " + stats.name) + '">' +
      '<button type="button" class="handle" id="sheet-handle" aria-label="Expand or collapse details" aria-controls="genre-pane-details" aria-expanded="' +
      (this._sheet === "full" ? "true" : "false") + '"></button>' +
      '<button type="button" class="pane-close" id="pane-close" title="Close (Esc)" aria-label="Close genre details">✕</button>' +
      '<div class="pane-title">' + this._esc(stats.name) + "</div>" +
      '<div class="pane-meta">' + this._esc(stats.meta + (updated ? " · updated " + updated : "")) + "</div>" +
      this._opHtml() +
      '<div class="peekrow"><div class="minis"><span>Tracks<b>' + stats.tracks + "</b></span><span>Pinned<b>" + stats.pinned +
      "</b></span><span>% lib<b>" + stats.pct + '</b></span><span class="m2">system1 ' + mix.system1 + "</span></div>" + bar +
      '<div class="peekbtns"><button type="button" class="act" id="peek-ask">✦ Ask AI</button>' +
      '<button type="button" class="act secondary" id="peek-ctx">Copy context</button>' +
      '<button type="button" class="act secondary" id="peek-more">Details ▴</button></div></div>' +
      '<div class="pfull" id="genre-pane-details">' +
      '<div class="stats"><div class="stat"><span class="lbl">Tracks</span><span class="val">' + stats.tracks +
      '</span></div><div class="stat"><span class="lbl">Pinned</span><span class="val">' + stats.pinned +
      '</span></div><div class="stat"><span class="lbl">% lib</span><span class="val">' + stats.pct + "</span></div></div>" +
      '<div><div class="sec">Classified by</div><div class="mix">' + mixBadges + "</div></div>" +
      '<div><div class="sec">' + (stats.parentName ? "Siblings in " + this._esc(stats.parentName) : "Children") + "</div>" + bar + kids + "</div>" +
      this._askHtml(this._livePage?.filter_counts.total ?? this._filtered().length) +
      actions + preview + retry +
      '<div class="keys"><span><span class="kbd">/</span> search</span><span><span class="kbd">Esc</span> close</span>' +
      '<span><span class="kbd">j</span> <span class="kbd">k</span> move</span><span><span class="kbd">Space</span> play</span>' +
      '<span><span class="kbd">Enter</span> expand</span></div>' +
      "</div></aside>"
    );
  }

  /** Working panel (spinner, one status line, progress bar, Cancel) or the failure banner. */
  _opHtml() {
    const op = this._op;
    if (!op) return "";
    if (controlsLocked(op)) {
      const pct = op.showCounts ? opPercent(op) : null;
      return (
        '<div class="op working" data-op-state="' + op.state + '">' +
        '<div class="oprow"><span class="spin" aria-hidden="true"></span><span class="opline" id="op-line">' +
        this._esc(opStatusLine(op)) + "</span>" +
        (op.cancellable
          ? '<button type="button" class="act secondary" id="op-cancel"' + (op.state === "cancelling" ? " disabled" : "") + ">Cancel</button>"
          : "") +
        "</div>" +
        (pct != null
          ? '<div class="bar" id="op-bar" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '" aria-valuetext="' + this._esc(opStatusLine(op)) + '"><i style="width:' + pct + '%"></i></div>'
          : "") +
        "</div>"
      );
    }
    const e = op.error || {};
    const cancelled = op.state === "cancelled";
    let when = e.at || "";
    try {
      when = new Date(e.at).toLocaleString();
    } catch (_) {
      /* keep the ISO string */
    }
    const canRetry = !!op.retry;
    return (
      '<div class="op ' + (cancelled ? "cancelled" : "failed") + '" role="group" aria-label="' + (cancelled ? "Cancelled" : "Failed") + ": " + this._esc(op.title) + '" id="op-banner" data-op-code="' + this._esc(e.code || "") + '">' +
      '<div class="ophd"><b>' + this._esc(cancelled ? "Cancelled" : e.cause || "Failed") + "</b> &middot; " + this._esc(op.title) + "</div>" +
      "<div>" + this._esc(e.message || "") + "</div>" +
      '<div class="note">' + this._esc(e.outcome || "") + "</div>" +
      '<div class="note">Request <code>' + this._esc(e.request_id || op.request_id) + "</code> &middot; " + this._esc(when) + "</div>" +
      '<div class="row">' +
      (canRetry ? '<button type="button" class="act" id="op-retry">Retry</button>' : "") +
      (cancelled ? "" : '<button type="button" class="act secondary" id="op-copy">Copy details</button>') +
      '<button type="button" class="act secondary" id="op-dismiss">Dismiss</button></div></div>'
    );
  }

  _render() {
    if (!this.shadowRoot) return;
    this._ensure();
    const root = this.shadowRoot;
    // Preserve focus, caret and scroll positions across re-renders.
    const active = root.activeElement;
    const focusSel = this._pendingFocus || this._selectorFor(active);
    this._pendingFocus = null;
    const focusId = active && active.id ? active.id : null;
    const focusTrigger = !focusId && active && active.getAttribute
      ? (["data-menu-copy", "data-menu-more"]
        .filter((attr) => active.hasAttribute && active.hasAttribute(attr))
        .map((attr) => [attr, active.getAttribute(attr)])[0] || null)
      : null;
    const sel = active && typeof active.selectionStart === "number" ? [active.selectionStart, active.selectionEnd] : null;
    const keep = {};
    for (const s of [".pane", ".chips.top", ".chips.sub", ".menu .mlist"]) {
      const el = root.querySelector ? root.querySelector(s) : null;
      if (el) keep[s] = [el.scrollTop, el.scrollLeft];
    }

    const ro = this._readOnly;
    const total = this._livePage?.counts.archive ?? (this._clips || []).length;
    const pinned = new Set(this._genres.pinned || []);
    const counts = this._livePage?.counts || honestCounts(this._genres, this._clips, this._blocked);
    const page = this._songPage();
    const base = this._filtered();
    const qc = this._livePage?.filter_counts || quickCounts(base, this._genres, this._tracks);
    const paneOpen = this._activeCategory && this._activeCategory !== "All";
    const stats = paneOpen ? this._paneStats(this._activeCategory) : null;
    const subParent = paneOpen ? subtierParent(this._genres, this._activeCategory) : null;

    const top = topChipRows(this._genres, total)
      .map((c) => {
        let k = "chip";
        if (c.name === "All" && this._activeCategory === "All") k += " active";
        else if (c.name === this._activeCategory) k += " selected";
        else if (subParent && subParent.name === c.name) k += " within";
        return '<button type="button" class="' + k + '" aria-pressed="' + (c.name === this._activeCategory ? "true" : "false") +
          '" data-cat="' + this._esc(c.name) + '">' + this._esc(c.name) +
          ' <span class="n">' + (c.count == null ? "" : c.count) + "</span></button>";
      })
      .join("");
    const sub = subParent
      ? '<div class="chips sub"><span class="sublbl">↳ in ' + this._esc(subParent.name) + "</span>" +
        subParent.children
          .filter((ch) => ch && ch.name)
          .map(
            (ch) =>
              '<button type="button" class="chip' + (ch.name === this._activeCategory ? " selected" : "") + '" aria-pressed="' + (ch.name === this._activeCategory ? "true" : "false") + '" data-cat="' +
              this._esc(ch.name) + '" title="' + this._esc(ch.name) + '">' + this._esc(shortChildName(subParent.name, ch.name)) +
              ' <span class="n">' + (ch.count || 0) + "</span></button>"
          )
          .join("") +
        "</div>"
      : "";

    const seg = [["newest", "Newest"], ["az", "A–Z"], ["bpm", "BPM"]]
      .map(([k, l]) => '<button type="button" data-sort="' + k + '" aria-pressed="' + (this._sort === k ? "true" : "false") + '" class="' + (this._sort === k ? "on" : "") + '">' + l + "</button>")
      .join("");
    const qf =
      '<button type="button" class="qf' + (this._qf.pinned ? " on" : "") + '" aria-pressed="' + !!this._qf.pinned + '" data-qf="pinned">Pinned <b>' + qc.pinned + "</b></button>" +
      '<button type="button" class="qf warn' + (this._qf.unsorted ? " on" : "") + '" aria-pressed="' + !!this._qf.unsorted + '" data-qf="unsorted">Unsorted <b>' + qc.unsorted + "</b></button>" +
      (this._tracksInfo.state === "ok"
        ? '<button type="button" class="qf' + (this._qf.audio ? " on" : "") + '" aria-pressed="' + !!this._qf.audio + '" data-qf="audio">Has audio <b>' + qc.audio + "</b></button>"
        : "");

    const list = page.songs.map((c) => this._cardHtml(c, pinned)).join("");
    const range = page.total
      ? (page.start + 1) + "–" + (page.start + page.songs.length) + " of " + page.total + " songs"
      : "0 songs";
    const pager = page.pages > 1
      ? '<nav class="row" aria-label="Song pages">' +
        '<button type="button" class="act secondary" data-song-page="prev"' +
        (this._page <= 1 ? " disabled" : "") + '>Previous</button>' +
        '<span id="song-page-status" tabindex="-1" role="status">' + range + '</span>' +
        '<button type="button" class="act secondary" data-song-page="next"' +
        (this._page >= page.pages ? " disabled" : "") + '>Next</button></nav>'
      : "";
    const orphanNote =
      counts.orphans > 0
        ? counts.orphans + " classified song" + (counts.orphans === 1 ? " isn't" : "s aren't") + " in the archive list yet"
        : "";
    const head =
      '<div class="hd"><h2>' + this._esc(this._config.title || "Suno archive") + "</h2>" +
      '<span class="counts"><span title="' + this._esc(orphanNote) + '"><b>' + counts.classified + "</b> classified</span> · <span><b>" +
      counts.archive + "</b> in archive</span>" +
      (counts.blocked != null ? ' · <span class="blk">' + counts.blocked + " blocked</span>" : "") +
      " · <span>" + counts.unsorted + " unsorted</span>" +
      (this._genres.model ? " · <span>" + this._esc(this._genres.model) + "</span>" : "") + "</span>" +
      (ro
        ? ""
        : '<div class="admin"><button type="button" class="ghost" id="toggle-edit">' + (this._editOpen ? "Done editing" : "Edit genres") +
          '</button><button type="button" class="ghost" id="add-cat"' + (this._editOpen && !this._locked ? "" : " disabled") + ">Add category</button>" +
          '<button type="button" class="ghost" id="reclassify"' + (this._locked ? " disabled" : "") + ">Reclassify</button>" +
          '<button type="button" class="ghost" id="undo"' + (this._locked ? " disabled" : "") + ">Undo</button></div>") +
      "</div>";

    const editRow =
      this._editOpen && !ro
        ? '<div class="row note">Editing: use ⋯ on a song to move or pin it; Rename / Delete appear in the genre pane.</div>'
        : "";

    if (!root.querySelector("#sg-app")) {
      root.innerHTML =
        "<style>" + STYLES + "</style>" +
        '<div id="sg-app"></div><div id="sg-live" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>' +
        '<div id="sg-alert" class="sr-only" role="alert" aria-live="assertive" aria-atomic="true"></div>';
    }
    root.querySelector("#sg-app").innerHTML =
      '<div class="wrap' + (ro ? " ro" : "") + '"' +
      (ro ? ' id="genre-keyboard-region" role="region" tabindex="0" aria-label="Music library keyboard shortcuts: J and K select songs, Space plays, Enter expands, slash searches"' : "") +
      '>' + head +
      (this._loading ? '<div class="note">Loading…</div>' : "") +
      (this._loadError ? '<div class="err">' + this._esc(this._loadError) +
        (this._usesLiveSongs() ? ' <button type="button" class="act secondary" id="retry-catalogue">Retry</button>' : "") + "</div>" : "") +
      (stats ? "" : this._opHtml()) +
      (this._err ? '<div class="err">' + this._esc(this._err) + "</div>" : "") +
      (this._saveMsg ? '<div class="ok">' + this._esc(this._saveMsg) + "</div>" : "") +
      editRow +
      '<div class="layout"><div class="main">' +
      '<div class="stick"><div class="chips top" id="top-chips">' + top + "</div>" +
      '<button type="button" class="more-chips" id="chips-more" aria-controls="top-chips" aria-expanded="' +
      (this._chipsOpen ? "true" : "false") + '" hidden>' + (this._chipsOpen ? "Less \u25b4" : "More \u25be") + "</button>" + sub + "</div>" +
      '<div class="filt"><label class="search">' + SEARCH_SVG +
      '<input type="search" id="q" maxlength="256" placeholder="search title or style…" value="' + this._esc(this._search) +
      '" aria-label="Search"/><span class="kbd">/</span></label>' +
      '<div class="seg" role="group" aria-label="Sort">' + seg + "</div>" + qf +
      '<span class="shown">' + page.total + " song" + (page.total === 1 ? "" : "s") + "</span></div>" +
      pager +
      '<div class="list">' + (list || (this._loading || this._loadError ? "" : '<div class="empty">No songs match this filter.</div>')) + "</div>" +
      (paneOpen ? "" : this._askHtml(this._livePage?.filter_counts.total ?? base.length)) +
      "</div>" + (stats ? this._paneHtml(stats) : "") + "</div></div>";

    for (const [s, v] of Object.entries(keep)) {
      const el = root.querySelector ? root.querySelector(s) : null;
      if (el) {
        el.scrollTop = v[0];
        el.scrollLeft = v[1];
      }
    }
    if (focusSel) {
      let el = null;
      try {
        el = root.querySelector(focusSel);
      } catch (_) {
        el = null;
      }
      if (el && el.focus) {
        el.focus({ preventScroll: focusSel !== "#genre-pane" });
        if (sel && el.setSelectionRange) {
          try {
            el.setSelectionRange(sel[0], sel[1]);
          } catch (_) {
            /* type=search may refuse */
          }
        }
      }
    }
    // Newer re-renders restore by element id; keep the older selector-based
    // path above for nodes without an id (chips, menu triggers).
    if (focusId && root.getElementById) {
      const el = root.getElementById(focusId);
      if (el && el.focus) {
        el.focus();
        if (sel && el.setSelectionRange) {
          try {
            el.setSelectionRange(sel[0], sel[1]);
          } catch (_) {
            /* type=search may refuse */
          }
        }
      }
    }
    if (focusTrigger && root.querySelectorAll) {
      const [attr, id] = focusTrigger;
      for (const candidate of root.querySelectorAll("[" + attr + "]")) {
        if (candidate.getAttribute(attr) === id) {
          candidate.focus();
          break;
        }
      }
    }
    this._syncChipsMore();
    this._wire();
    this._updateProgress();
    this._syncLive();
  }

  /** A selector that finds `el` again after the next re-render (id, else its first data-* hook). */
  _selectorFor(el) {
    if (!el || !el.getAttribute) return null;
    if (el.id) return "#" + CSS_ESCAPE(el.id);
    for (const a of Array.from(el.attributes || [])) {
      if (a.name.startsWith("data-") && a.name !== "data-card")
        return el.tagName.toLowerCase() + "[" + a.name + '="' + String(a.value).replace(/["\\]/g, "\\$&") + '"]';
    }
    return null;
  }

  /**
   * Screen-reader announcements live in two persistent regions that survive
   * re-renders (a region that is re-created with its text is not reliably
   * announced): #sg-live (polite) for progress / results and #sg-alert (assertive)
   * for a failure, spoken once per failed request and not again on every re-render.
   */
  _syncLive() {
    const root = this.shadowRoot;
    const live = root && root.querySelector("#sg-live");
    const alertEl = root && root.querySelector("#sg-alert");
    if (!live || !alertEl) return;
    const op = this._op;
    let msg = "";
    if (op && controlsLocked(op)) msg = opStatusLine(op);
    else if (op && op.state === "cancelled") msg = "Cancelled. " + (op.title || "");
    else if (!op && this._liveNote) msg = this._liveNote;
    if (msg !== this._liveMsg) {
      this._liveMsg = msg;
      live.textContent = msg;
    }
    const e = op && op.state === "failed" ? op.error : null;
    const key = e ? e.request_id || op.request_id || "failed" : "";
    if (key !== this._alertKey) {
      this._alertKey = key;
      alertEl.textContent = e ? (e.cause || "Failed") + ". " + (e.message || "") + " " + (e.outcome || "") : "";
    }
  }

  /** #133: narrow layouts clamp the top chip row; reveal the rest on demand. */
  _syncChipsMore() {
    const root = this.shadowRoot;
    if (!root || !root.querySelector) return;
    const box = root.querySelector(".chips.top");
    const btn = root.querySelector("#chips-more");
    if (!box || !btn) return;
    const open = !!this._chipsOpen;
    box.classList.toggle("open", open);
    if (!open) this._chipsOverflow = box.scrollHeight - box.clientHeight > 2;
    btn.hidden = !(open || this._chipsOverflow);
    btn.textContent = open ? "Less \u25b4" : "More \u25be";
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? "Show fewer genres" : "Show all genres");
  }

  _focusMenuTrigger(menu) {
    if (!menu || !this.shadowRoot || !this.shadowRoot.querySelectorAll) return;
    const attr = menu.kind === "copy" ? "data-menu-copy" : "data-menu-more";
    for (const button of this.shadowRoot.querySelectorAll("[" + attr + "]")) {
      if (button.getAttribute(attr) === menu.id) {
        button.focus();
        return;
      }
    }
  }

  _focusFirstMenuAction() {
    this.shadowRoot?.querySelector(".menu button:not([disabled])")?.focus();
  }

  _wire() {
    const root = this.shadowRoot;
    if (this._delegated || !root.addEventListener) return;
    this._delegated = true;
    root.addEventListener("click", (e) => this._onClick(e));
    root.addEventListener("input", (e) => {
      const t = e.target;
      if (!t) return;
      if (t.id === "q") {
        this._search = t.value;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this._render(), 140);
      } else if (t.id === "prompt") {
        this._prompt = t.value;
      }
    });
    root.addEventListener("pointerdown", (e) => {
      const h = e.target && e.target.closest ? e.target.closest("#sheet-handle") : null;
      if (!h) return;
      this._drag = { y: e.clientY, moved: false };
    });
    root.addEventListener("pointerup", (e) => {
      if (!this._drag) return;
      const dy = e.clientY - this._drag.y;
      this._drag = null;
      if (Math.abs(dy) < 8) return; // a tap: handled by click
      this._dragHandled = true;
      if (dy < 0) this._sheet = "full";
      else if (this._sheet === "full") this._sheet = "peek";
      else this._activeCategory = "All";
      this._render();
    });
  }

  _onClick(e) {
    const t = e.target;
    if (!t || !t.closest) return;
    const hit = (sel) => t.closest(sel);
    const attr = (el, name) => (el ? el.getAttribute(name) : null);
    const clipById = (id) => (this._clips || []).find((c) => c.id === id);
    let el;
    // Close an open menu on any click outside it / its trigger.
    if (this._menu && !hit(".menu") && !hit("[data-menu-copy]") && !hit("[data-menu-more]")) {
      this._menu = null;
      if (!hit("button") && !hit("a")) {
        this._render();
        return;
      }
    }
    if ((el = hit("[data-cat]"))) {
      const name = attr(el, "data-cat");
      const r = this._selectCategory(name, e.detail === 0 ? "#genre-pane" : null);
      return r;
    }
    if ((el = hit("#chips-more"))) {
      this._chipsOpen = !this._chipsOpen;
      return this._render();
    }
    if ((el = hit("[data-song-page]"))) {
      if (el.disabled) return;
      this._page += attr(el, "data-song-page") === "next" ? 1 : -1;
      this._focusId = null;
      if (this._usesLiveSongs()) this._focusPage = true;
      else this._pendingFocus = "#song-page-status";
      return this._render();
    }
    if ((el = hit("[data-sort]"))) {
      this._sort = attr(el, "data-sort");
      return this._render();
    }
    if ((el = hit("[data-qf]"))) {
      const k = attr(el, "data-qf");
      this._qf[k] = !this._qf[k];
      return this._render();
    }
    if ((el = hit("[data-play]"))) {
      const id = attr(el, "data-play");
      this._focusId = id;
      return this._togglePlay(id);
    }
    if ((el = hit("[data-expand]"))) {
      const id = attr(el, "data-expand");
      this._focusId = id;
      return this._toggleExpand(id);
    }
    if ((el = hit("[data-copy-song]"))) {
      const c = clipById(attr(el, "data-copy-song"));
      if (c) this._copySong(c, "all", el);
      return;
    }
    if ((el = hit("[data-copy-kind]"))) {
      const c = clipById(attr(el, "data-song"));
      if (c) this._copySong(c, attr(el, "data-copy-kind"), null);
      return;
    }
    if ((el = hit("[data-menu-copy]")) || (el = hit("[data-menu-more]"))) {
      const id = attr(el, "data-menu-copy") || attr(el, "data-menu-more");
      const kind = el.hasAttribute("data-menu-copy") ? "copy" : "more";
      const same = this._menu && this._menu.id === id && this._menu.kind === kind;
      this._menu = same ? null : { id, kind, sub: null };
      this._focusId = id;
      this._render();
      if (this._menu) this._focusFirstMenuAction();
      return;
    }
    if ((el = hit("[data-menu-move]"))) {
      this._menu = { id: attr(el, "data-menu-move"), kind: "more", sub: "move" };
      this._render();
      this._focusFirstMenuAction();
      return;
    }
    if (hit("[data-menu-back]")) {
      if (this._menu) this._menu.sub = null;
      this._render();
      this._focusFirstMenuAction();
      return;
    }
    if ((el = hit("[data-move-to]"))) {
      const id = attr(el, "data-song");
      const openedMenu = this._menu;
      this._menu = null;
      return this._moveSong(id, attr(el, "data-move-to")).then(
        () => this._focusMenuTrigger(openedMenu));
    }
    if ((el = hit("[data-pin]"))) return this._togglePin(attr(el, "data-pin"));
    if ((el = hit("[data-open]"))) {
      this._menu = null;
      if (typeof window !== "undefined" && window.open)
        window.open("https://suno.com/song/" + encodeURIComponent(attr(el, "data-open")), "_blank", "noopener");
      return this._render();
    }
    if ((el = hit("button[id]"))) return this._onButton(el.id, el);
  }

  async _onButton(id, el) {
    const cat = this._activeCategory;
    switch (id) {
      case "retry-catalogue":
        return this._loadLivePage(true);
      case "sheet-handle":
        if (this._dragHandled) {
          this._dragHandled = false;
          return;
        }
        this._sheet = this._sheet === "full" ? "peek" : "full";
        return this._render();
      case "pane-close":
        return this._selectCategory("All");
      case "peek-more":
        this._sheet = "full";
        return this._render();
      case "peek-ask": {
        this._sheet = "full";
        this._render();
        const p = this.shadowRoot.querySelector("#ask-block");
        if (p && p.scrollIntoView) p.scrollIntoView({ block: "nearest" });
        const ta = this.shadowRoot.querySelector("#prompt");
        if (ta && ta.focus) ta.focus();
        return;
      }
      case "peek-ctx":
      case "copy-context": {
        const ctx = this._ctxForFallback && this._ctxCat === cat ? this._ctxForFallback : await this._contextForAI(await this._contextClips());
        if (!ctx.used.length) {
          this._err = "Nothing to copy — no songs in the current filter.";
          return this._render();
        }
        this._ctxForFallback = ctx;
        this._ctxCat = cat;
        return this._copy(clipboardContext(ctx), el);
      }
      case "op-cancel":
        opCancel(this._op);
        return this._render();
      case "op-retry": {
        const retry = this._op && this._op.retry;
        const err = this._op && this._op.error;
        this._op = null;
        this._render();
        return retry ? retry(err) : undefined;
      }
      case "op-copy":
        return this._copy(redactSecrets(failureDetails(this._op)), el);
      case "op-dismiss":
        this._op = null;
        return this._render();
      case "send":
        return this._sendAI();
      case "copy":
        return this._copy(this._aiResponse || "", el);
      case "toggle-edit":
        this._editOpen = !this._editOpen;
        return this._render();
      case "add-cat":
        return this._addCategory();
      case "reclassify":
        return this._reclassify();
      case "undo":
        return this._undo();
      case "apply-split":
        return this._applySplit();
      case "cancel-split":
        this._splitPreview = null;
        this._splitRetryable = false;
        return this._render();
      case "retry-split":
        return this._retrySplit();
      case "pane-split":
        if (cat && cat !== "All") return this._splitWithAI(cat);
        return;
      case "pane-edit":
        this._editOpen = !this._editOpen;
        return this._render();
      case "pane-rename":
        if (cat && cat !== "All") return this._renameCategory(cat);
        return;
      case "pane-delete":
        if (cat && cat !== "All") return this._deleteCategory(cat);
        return;
      case "pane-clear":
        if (!cat || cat === "All") return;
        if (!confirm('Clear "' + cat + '"? Its songs move to Uncategorised and are pinned.')) return;
        return this._clearCategory(cat);
      default:
        return;
    }
  }

  getCardSize() {
    return 12;
  }
}

if (!customElements.get("suno-genre-card")) customElements.define("suno-genre-card", SunoGenreCard);
if (typeof window !== "undefined") {
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === "suno-genre-card"))
    window.customCards.push({
      type: "suno-genre-card",
      name: "Suno Genre Browser",
      description: "Genre browser with detail pane, play, lyrics, Ask AI and in-pane split/edit",
      preview: false,
    });
}

export { SunoGenreCard, parentMap };
