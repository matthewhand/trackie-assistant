/**
 * Suno Genre Browser — pure helpers shared by the Lovelace card and node tests.
 *
 * No DOM, no fetch, no HA imports: everything here is a plain function over
 * plain data so `tests/js/test_suno_genre_core.mjs` can exercise it directly.
 */

export const DEFAULT_AGENT_ID = "conversation.litellm";
export const MIN_SPLIT_SONGS = 4;
export const MIN_SUBCATEGORIES = 2;
export const MAX_SUBCATEGORIES = 4;
export const MAX_SUBTIER_ASSIGNMENT_CHUNK = 12;

/** Thrown when an agent reply cannot be read as JSON. Carries a reply snippet. */
export class ReplyParseError extends Error {
  constructor(label, raw, detail) {
    const snippet = replySnippet(raw);
    super(
      label +
        ": the agent did not return JSON" +
        (detail ? " (" + detail + ")" : "") +
        ". Reply was: " +
        (snippet ? '"' + snippet + '"' : "(empty reply)")
    );
    this.name = "ReplyParseError";
    this.label = label;
    this.raw = typeof raw === "string" ? raw : "";
    this.snippet = snippet;
    this.retryable = true;
  }
}

export function replySnippet(raw, max = 160) {
  const text = String(raw == null ? "" : raw)
    .replace(/```[a-zA-Z]*\n?/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

/** True when a reply looks like a model refusal rather than content. */
export function isRefusalText(raw) {
  const t = String(raw == null ? "" : raw).toLowerCase();
  if (!t.trim()) return false;
  const patterns = [
    "i can't help",
    "i cannot help",
    "i can’t help",
    "i cant help",
    "i can't assist",
    "i cannot assist",
    "i can't do that",
    "i cannot do that",
    "i'm sorry",
    "i am sorry",
    "i apologize",
    "i apologise",
    "i won't",
    "i will not",
    "i must decline",
    "i have to decline",
    "unable to help",
    "not able to help",
    "can't comply",
    "cannot comply",
    "against my guidelines",
    "not appropriate for me",
  ];
  return patterns.some((p) => t.includes(p));
}

/** Pull the assistant utterance out of a conversation.process response. */
export function extractSpeech(result) {
  const resp =
    (result && result.response) ||
    (result && result.result && result.result.response) ||
    result;
  if (!resp) return "";
  const candidates = [
    () => resp.response && resp.response.speech && resp.response.speech.plain && resp.response.speech.plain.speech,
    () => resp.speech && resp.speech.plain && resp.speech.plain.speech,
    () => resp.response && resp.response.speech && resp.response.speech.plain,
    () => resp.speech && resp.speech.plain,
  ];
  for (const get of candidates) {
    try {
      const v = get();
      if (typeof v === "string" && v.trim()) return v;
    } catch (_) {
      /* shape mismatch — try the next accessor */
    }
  }
  return typeof resp === "string" ? resp : JSON.stringify(resp);
}

/** Like extractSpeech, but "" when the response carries no speech at all (no JSON fallback). */
export function extractSpeechStrict(result) {
  const resp =
    (result && result.response) ||
    (result && result.result && result.result.response) ||
    result;
  if (!resp) return "";
  if (typeof resp === "string") return resp;
  const candidates = [
    () => resp.response && resp.response.speech && resp.response.speech.plain && resp.response.speech.plain.speech,
    () => resp.speech && resp.speech.plain && resp.speech.plain.speech,
    () => resp.response && resp.response.speech && resp.response.speech.plain,
    () => resp.speech && resp.speech.plain,
  ];
  for (const get of candidates) {
    try {
      const v = get();
      if (typeof v === "string" && v.trim()) return v;
    } catch (_) {
      /* shape mismatch - try the next accessor */
    }
  }
  return "";
}

function stripFences(text) {
  const blocks = [];
  const re = /```([a-zA-Z0-9_-]*)\s*([\s\S]*?)```/g;
  let m;
  while ((m = re.exec(text)) !== null) blocks.push(m[2]);
  return { blocks, rest: text.replace(re, "\n") };
}

/** Every balanced {...} / [...] span in `text`, longest first. */
function jsonCandidates(text) {
  const out = [];
  const openers = { "{": "}", "[": "]" };
  for (let i = 0; i < text.length; i++) {
    const opener = text[i];
    if (!openers[opener]) continue;
    const closer = openers[opener];
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let j = i; j < text.length; j++) {
      const ch = text[j];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === opener) depth++;
      else if (ch === closer) {
        depth--;
        if (depth === 0) {
          out.push(text.slice(i, j + 1));
          i = j;
          break;
        }
      }
    }
  }
  return out.sort((a, b) => b.length - a.length);
}

function repairJson(text) {
  return text
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/,\s*([}\]])/g, "$1")
    .replace(/(\{|,)\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:/g, '$1"$2":');
}

/**
 * Read a JSON object out of an agent reply.
 *
 * Tolerates: raw JSON, fenced JSON (```json), prose before/after the object,
 * several objects in one reply (longest/most complete wins), smart quotes and
 * trailing commas. Throws {@link ReplyParseError} — never a bare "No JSON" —
 * when the reply is prose, a refusal, or empty.
 */
export function parseJsonReply(raw, label = "Agent reply") {
  const text = String(raw == null ? "" : raw).trim();
  if (!text) throw new ReplyParseError(label, raw, "empty reply");
  if (isRefusalText(text)) throw new ReplyParseError(label, raw, "the agent refused");

  const { blocks, rest } = stripFences(text);
  const attempts = [];
  attempts.push(text);
  if (blocks.length) attempts.push(...blocks);
  attempts.push(rest);

  for (const attempt of attempts) {
    const trimmed = String(attempt).trim();
    if (!trimmed) continue;
    try {
      return JSON.parse(trimmed);
    } catch (_) {
      /* keep trying */
    }
  }

  const spans = [];
  for (const attempt of attempts) spans.push(...jsonCandidates(String(attempt)));
  for (const span of spans) {
    try {
      return JSON.parse(span);
    } catch (_) {
      try {
        return JSON.parse(repairJson(span));
      } catch (_) {
        /* keep trying */
      }
    }
  }

  throw new ReplyParseError(label, raw, spans.length ? "JSON was malformed" : "no JSON object in reply");
}

/** Stricter follow-up prompt used for the single Split retry. */
export function strictJsonPrompt(original) {
  return (
    original +
    "\n\nSTRICT FORMAT RULES (this is a machine-parsing task, not a conversation):\n" +
    "Reply with exactly one JSON object and nothing else. No preamble, no explanation,\n" +
    "no markdown fences, no apology, no refusal. Example of a valid reply:\n" +
    '{"subcategories":[{"name":"Name","description":"Short description"}]}'
  );
}

/** All-first taxonomy bootstrap. Never invents categories; never drops data. */
export function ensureGenres(raw) {
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const genres = Object.assign({}, src);
  if (!Array.isArray(genres.categories)) genres.categories = [];
  else genres.categories = genres.categories.filter((c) => c && typeof c === "object");
  if (!genres.assignments || typeof genres.assignments !== "object" || Array.isArray(genres.assignments))
    genres.assignments = {};
  if (!Array.isArray(genres.pinned)) genres.pinned = [];
  if (!genres.placement || typeof genres.placement !== "object" || Array.isArray(genres.placement))
    genres.placement = {};
  return genres;
}

/** The All-first chip always exists, even with zero categories. */
export function buildChipRows(genres, total) {
  const rows = [{ name: "All", count: total, parent: null, splittable: false }];
  for (const c of (genres && genres.categories) || []) {
    if (!c || !c.name) continue;
    if (Array.isArray(c.children) && c.children.length) {
      rows.push({ name: c.name, count: c.count || 0, parent: null, splittable: false });
      for (const ch of c.children)
        if (ch && ch.name)
          rows.push({ name: ch.name, count: ch.count || 0, parent: c.name, splittable: false });
    } else {
      rows.push({ name: c.name, count: c.count || 0, parent: null, splittable: true });
    }
  }
  return rows;
}

export function leafNames(genres) {
  const names = [];
  for (const c of (genres && genres.categories) || []) {
    if (!c || !c.name) continue;
    if (Array.isArray(c.children) && c.children.length) {
      for (const ch of c.children) if (ch && ch.name) names.push(ch.name);
    } else names.push(c.name);
  }
  return names;
}

export function parentMap(genres) {
  const map = {};
  for (const c of (genres && genres.categories) || [])
    if (c && Array.isArray(c.children))
      for (const ch of c.children) if (ch && ch.name) map[ch.name] = c.name;
  return map;
}

/** Recount every category from assignments. Mutates and returns `genres`. */
export function recount(genres) {
  const g = ensureGenres(genres);
  const values = Object.values(g.assignments);
  for (const c of g.categories) {
    if (Array.isArray(c.children) && c.children.length) {
      for (const ch of c.children)
        ch.count = values.filter((v) => v === ch.name).length;
      // Songs pinned to the parent itself (e.g. kept aside during a split)
      // still belong to the parent, so count them alongside the subtiers.
      c.count =
        values.filter((v) => v === c.name).length +
        c.children.reduce((sum, ch) => sum + (ch.count || 0), 0);
    } else {
      c.count = values.filter((v) => v === c.name).length;
    }
  }
  return g;
}

export function matchesCategory(leaf, category, parents) {
  if (!category || category === "All") return true;
  if (!leaf) return false;
  return leaf === category || parents[leaf] === category;
}

/** Clips for a category (a parent matches its whole subtier). */
export function clipsInCategory(clips, genres, category) {
  const assignments = (genres && genres.assignments) || {};
  const parents = parentMap(genres);
  return (clips || []).filter((c) => matchesCategory(assignments[c.id], category, parents));
}

/** Current filter: category + free-text search over title and style. */
export function filterClips(clips, genres, category, search) {
  const assignments = (genres && genres.assignments) || {};
  const parents = parentMap(genres);
  const q = String(search || "").trim().toLowerCase();
  return (clips || []).filter((c) => {
    if (!matchesCategory(assignments[c.id], category, parents)) return false;
    if (!q) return true;
    return ((c.title || "") + " " + (c.style || "")).toLowerCase().includes(q);
  });
}

/** Normalise an agent taxonomy reply into 2-4 named subcategories. */
export function normalizeSubcategories(parsed) {
  const raw = (parsed && parsed.subcategories) || (parsed && parsed.categories) || [];
  const list = Array.isArray(raw) ? raw : [];
  const seen = new Set();
  const out = [];
  for (const item of list) {
    const name = String((item && (item.name || item.title || item.label)) || "").trim();
    if (!name) continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ name, description: String((item && item.description) || "").trim() });
    if (out.length >= MAX_SUBCATEGORIES) break;
  }
  return out;
}

/**
 * Merge one batch of assignment replies. Unknown names are dropped and
 * unmapped songs fall back to the last subcategory so every song lands somewhere.
 */
export function mergeAssignments(parsed, names, target, fallbackIndex) {
  const lower = new Map();
  names.forEach((n, i) => lower.set(n.toLowerCase(), i));
  const part = (parsed && parsed.assignments) || {};
  const fallback = names[fallbackIndex == null ? names.length - 1 : fallbackIndex];
  for (const [id, value] of Object.entries(part)) {
    const idx = lower.get(String(value).trim().toLowerCase());
    if (idx === undefined) continue;
    target[id] = names[idx];
  }
  return { assignments: target, fallback };
}

export function countAssignments(assignments, names) {
  const counts = {};
  for (const n of names) counts[n] = 0;
  for (const value of Object.values(assignments || {})) {
    if (Object.prototype.hasOwnProperty.call(counts, value)) counts[value] += 1;
  }
  return counts;
}

/**
 * Apply a Split preview onto a copy of the taxonomy.
 * Pinned songs keep their existing assignment; nothing else is dropped.
 */
export function applySplitPreview(genres, preview) {
  if (!preview || !preview.parent || !Array.isArray(preview.subcategories))
    throw new Error("Split: nothing to apply");
  const g = JSON.parse(JSON.stringify(ensureGenres(genres)));
  const pinned = new Set(g.pinned);
  const children = preview.subcategories.map((s) => ({
    name: s.name,
    description: s.description || "",
    count: s.count || 0,
  }));
  for (const [id, leaf] of Object.entries(preview.assignments || {})) {
    if (pinned.has(id)) continue;
    g.assignments[id] = leaf;
    g.placement[id] = "llm";
  }
  const existing = g.categories.find((c) => c && c.name === preview.parent);
  if (existing) {
    existing.children = children;
    existing.count = children.reduce((sum, ch) => sum + (ch.count || 0), 0);
  } else {
    g.categories.push({
      name: preview.parent,
      description: "",
      count: children.reduce((sum, ch) => sum + (ch.count || 0), 0),
      children,
    });
  }
  return recount(g);
}

/**
 * Pack title/style/lyrics context for the conversation agent.
 * `used` records which clips made the cut so the caller can report truncation.
 */
export function packAiContext(clips, lyricsById, options) {
  const opts = Object.assign({ maxChars: 150000, perSongChars: 2000 }, options || {});
  const used = [];
  let chars = 0;
  let truncated = false;
  for (const clip of clips || []) {
    const lyrics = String(
      (lyricsById && lyricsById[clip.id]) || clip.lyrics || ""
    ).slice(0, opts.perSongChars);
    const block =
      "Title: " +
      (clip.title || "") +
      "\nStyle: " +
      (clip.style || "") +
      "\nLyrics:\n" +
      lyrics +
      "\n---\n";
    if (chars + block.length > opts.maxChars) {
      truncated = true;
      break;
    }
    used.push({ clip, lyrics, block });
    chars += block.length;
  }
  const text = used.map((x) => x.block.replace(/\n---\n$/, "")).join("\n---\n");
  return { text, used, chars, truncated, lyricsBySong: used.length };
}

/** Plain-text context block used by the Ask AI clipboard fallback. */
export function clipboardContext(context) {
  const parts = [
    "Suno archive context — " + context.used.length + " song(s), ~" + context.chars + " chars",
    context.truncated ? "(truncated to fit the agent context window)" : "",
    "",
  ];
  for (const { clip, lyrics } of context.used)
    parts.push(
      "Title: " + (clip.title || "") + "\nStyle: " + (clip.style || "") + "\nLyrics:\n" + lyrics + "\n---"
    );
  return parts.filter((p) => p !== undefined).join("\n").trim() + "\n";
}

export function summariseContext(context) {
  return context.truncated
    ? "Truncated to " + context.used.length + " songs (~" + context.chars + " chars)."
    : "~" + context.chars + " chars, " + context.lyricsBySong + " songs with lyrics fetched.";
}
/* ───────────── v6 helpers: sort, filters, counts, pane stats ───────────── */

export const SHARE_COLORS = ["#3b82f6", "#8b5cf6", "#14b8a6", "#f59e0b", "#ec4899", "#64748b"];

/** First "NNN BPM" figure in a style string, or null. */
export function parseBpm(style) {
  const m = /(\d{2,3})\s*BPM/i.exec(String(style || ""));
  if (!m) return null;
  const n = Number(m[1]);
  return n >= 40 && n <= 300 ? n : null;
}

/** "m:ss" for a duration in seconds; "" when unknown. */
export function fmtDuration(sec) {
  const s = Number(sec);
  if (!Number.isFinite(s) || s <= 0) return "";
  const total = Math.round(s);
  return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
}

/** "6 Oct" (adds the year when it differs from `now`). "" when unparsable. */
export function fmtDate(iso, now) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const ref = now ? new Date(now) : new Date();
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== ref.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-AU", opts);
}

/** "7 Oct, 5:53 am" for a timestamp; "" when unparsable. */
export function fmtStamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return (
    d.toLocaleDateString("en-AU", { day: "numeric", month: "short" }) +
    ", " +
    d.toLocaleTimeString("en-AU", { hour: "numeric", minute: "2-digit" })
  );
}

/** Sort a copy of `clips`: "newest" (created desc), "az" (title), "bpm" (desc). */
export function sortClips(clips, mode, tracks) {
  const list = (clips || []).slice();
  const meta = tracks || {};
  if (mode === "az") {
    return list.sort((a, b) =>
      String(a.title || "").localeCompare(String(b.title || ""), "en", { sensitivity: "base" })
    );
  }
  if (mode === "bpm") {
    return list.sort((a, b) => (parseBpm(b.style) || -1) - (parseBpm(a.style) || -1));
  }
  // newest: tracks with a created date first (desc); the rest keep archive order.
  const idx = new Map(list.map((c, i) => [c.id, i]));
  const ts = (c) => {
    const t = meta[c.id] && meta[c.id].created ? Date.parse(meta[c.id].created) : NaN;
    return Number.isNaN(t) ? null : t;
  };
  return list.sort((a, b) => {
    const ta = ts(a);
    const tb = ts(b);
    if (ta != null && tb != null && ta !== tb) return tb - ta;
    if (ta != null && tb == null) return -1;
    if (ta == null && tb != null) return 1;
    return idx.get(a.id) - idx.get(b.id);
  });
}

/** True when a song is unsorted: classifier abstained or parked in Uncategorised. */
export function isUnsorted(genres, id) {
  const g = genres || {};
  const placement = (g.placement || {})[id] || "";
  const leaf = (g.assignments || {})[id] || "";
  return /abstain/i.test(placement) || leaf === "Uncategorised" || !leaf;
}

/** Apply quick filters {pinned, unsorted, audio}. `tracks` is the /api/tracks map. */
export function quickFilter(clips, genres, tracks, filters) {
  const f = filters || {};
  const pinned = new Set((genres && genres.pinned) || []);
  const meta = tracks || {};
  return (clips || []).filter((c) => {
    if (f.pinned && !pinned.has(c.id)) return false;
    if (f.unsorted && !isUnsorted(genres, c.id)) return false;
    if (f.audio && !(meta[c.id] && meta[c.id].audio)) return false;
    return true;
  });
}

/** Counts for the quick-filter pills over a clip list. */
export function quickCounts(clips, genres, tracks) {
  const pinned = new Set((genres && genres.pinned) || []);
  const meta = tracks || {};
  let p = 0;
  let u = 0;
  let a = 0;
  for (const c of clips || []) {
    if (pinned.has(c.id)) p++;
    if (isUnsorted(genres, c.id)) u++;
    if (meta[c.id] && meta[c.id].audio) a++;
  }
  return { pinned: p, unsorted: u, audio: a };
}

/** How the songs in a list were placed: system1 / llm / abstained / manual (+ pinned). */
export function classifierMix(clips, genres) {
  const placement = (genres && genres.placement) || {};
  const pinned = new Set((genres && genres.pinned) || []);
  const mix = { system1: 0, llm: 0, abstained: 0, manual: 0, other: 0, pinned: 0 };
  for (const c of clips || []) {
    const p = String(placement[c.id] || "");
    if (/abstain/i.test(p)) mix.abstained++;
    else if (/^system1/i.test(p)) mix.system1++;
    else if (/llm/i.test(p)) mix.llm++;
    else if (/manual/i.test(p)) mix.manual++;
    else mix.other++;
    if (pinned.has(c.id)) mix.pinned++;
  }
  return mix;
}

/** Share of each child (or sibling, for a subtier) with a stable colour. */
export function childShares(genres, name) {
  let parent = null;
  for (const c of (genres && genres.categories) || []) {
    if (!c) continue;
    if (c.name === name && Array.isArray(c.children) && c.children.length) parent = c;
    else if ((c.children || []).some((ch) => ch && ch.name === name)) parent = c;
  }
  if (!parent) return [];
  const kids = parent.children.filter((ch) => ch && ch.name);
  const total = kids.reduce((s, ch) => s + (ch.count || 0), 0) || 1;
  return kids.map((ch, i) => ({
    name: ch.name,
    count: ch.count || 0,
    pct: Math.round((100 * (ch.count || 0)) / total),
    color: SHARE_COLORS[i % SHARE_COLORS.length],
    current: ch.name === name,
  }));
}

/** Why Split is unavailable, as display text; "" when Split is allowed. */
export function splitReason(stats, opts) {
  const o = opts || {};
  if (o.readOnly) return "Read-only here: split, edit and clear genres on the Home Assistant card.";
  if (!stats) return "";
  if (stats.isParent)
    return (
      "Split unavailable: already split into " +
      stats.children.length +
      " subtier" +
      (stats.children.length === 1 ? "" : "s") +
      ". Pick a subtier to edit or move its songs."
    );
  if (stats.parentName)
    return "Split unavailable: subtiers of " + stats.parentName + " can't be split again.";
  if (stats.tracks < MIN_SPLIT_SONGS)
    return "Split needs at least " + MIN_SPLIT_SONGS + " songs (this genre has " + stats.tracks + ").";
  if (o.noAgent) return "Split needs Home Assistant's conversation agent, which isn't connected.";
  return "";
}

/** Honest header counts. `blocked` comes from suno-dl (null when unknown). */
export function honestCounts(genres, clips, blocked) {
  const assignments = (genres && genres.assignments) || {};
  const ids = new Set((clips || []).map((c) => c.id));
  const classified = Object.keys(assignments).length;
  const orphans = Object.keys(assignments).filter((id) => !ids.has(id)).length;
  let unsorted = 0;
  for (const c of clips || []) if (isUnsorted(genres, c.id)) unsorted++;
  return {
    classified,
    archive: (clips || []).length,
    orphans,
    blocked: blocked == null ? null : Number(blocked) || 0,
    unsorted,
  };
}

/** Top-level chips only (parents + standalone leaves), All first. */
export function topChipRows(genres, total) {
  const rows = [{ name: "All", count: total, hasChildren: false }];
  for (const c of (genres && genres.categories) || []) {
    if (!c || !c.name) continue;
    rows.push({
      name: c.name,
      count: c.count || 0,
      hasChildren: Array.isArray(c.children) && c.children.length > 0,
    });
  }
  return rows;
}

/** The parent whose subtier row should show for `active` (itself or its parent). */
export function subtierParent(genres, active) {
  for (const c of (genres && genres.categories) || []) {
    if (!c || !Array.isArray(c.children) || !c.children.length) continue;
    if (c.name === active || c.children.some((ch) => ch && ch.name === active)) return c;
  }
  return null;
}

/** Strip a common "<Parent>-" / "<Parent> " prefix so subtier chips read short. */
export function shortChildName(parentName, childName) {
  const p = String(parentName || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  const words = String(childName || "").split(/[\s-]+/);
  for (let i = words.length - 1; i > 0; i--) {
    const head = words.slice(0, i).join("").toLowerCase().replace(/[^a-z0-9]+/g, "");
    if (head === p) return words.slice(i).join(" ");
  }
  return childName;
}

/* ───────────────────── #95: long-action status + structured errors ─────────────────────
 * Pure helpers (no DOM) shared by the HA card and the suno-dl web page. The
 * codes below match src/suno_dl/opstatus.py so a backend error envelope
 * {code, message, detail, request_id} and a browser-side failure read alike.
 */

export const AGENT_TIMEOUT_MS = 120000;

export const CAUSES = {
  ai_timeout: "AI service timeout",
  ai_unreachable: "AI service unreachable",
  ai_invalid_reply: "AI service sent an invalid reply",
  ai_empty_reply: "AI service sent an empty reply",
  file_permission: "File or permission problem",
  not_connected: "Home Assistant not connected",
  busy: "Another action is already running",
  cancelled: "Cancelled",
  unexpected: "Unexpected error",
};

export class OpTimeoutError extends Error {
  constructor(label, ms) {
    super(label + " did not answer within " + Math.round(ms / 1000) + " s (timed out)");
    this.name = "OpTimeoutError";
  }
}

export class OpCancelledError extends Error {
  constructor() {
    super("Cancelled by you");
    this.name = "OpCancelledError";
  }
}

/** Short id the user can quote; also sent with client-error reports. */
export function newRequestId(rnd = Math.random) {
  let out = "";
  for (let i = 0; i < 8; i++) out += Math.floor(rnd() * 16).toString(16);
  return out;
}

/** Mask tokens, keys, cookies and credentials in free text (mirrors opstatus.redact). */
export function redactSecrets(text) {
  let out = String(text == null ? "" : text);
  out = out.replace(/\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi, "$1 [redacted]");
  out = out.replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}/g, "[redacted-jwt]");
  out = out.replace(/\b(sk|pk|rk|ghp|gho|ghs|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{12,}/g, "[redacted-key]");
  out = out.replace(
    /\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|cookie|authorization)\b(\s*[:=]\s*)([^\s,;&"']+)/gi,
    "$1$2[redacted]"
  );
  out = out.replace(/([?&](?:token|key|api_key|access_token|sig|signature)=)[^&\s]+/gi, "$1[redacted]");
  return out;
}

/** Turn a non-OK fetch response (and its JSON envelope, if any) into an Error. */
export function apiErrorFrom(status, body, label) {
  const env = body && typeof body === "object" ? body : {};
  const e = new Error(env.message || env.error || (label || "Request") + " failed with HTTP " + status);
  e.name = "ApiError";
  e.status = status;
  e.api = {
    code: typeof env.code === "string" ? env.code : "",
    message: env.message || env.error || "",
    detail: env.detail || "",
    request_id: env.request_id || "",
  };
  return e;
}

/** Race ``promise`` against a timeout and (optionally) an op's cancel signal. */
export function withTimeout(promise, ms, label, op) {
  let timer;
  const racers = [
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new OpTimeoutError(label || "The service", ms)), ms);
    }),
  ];
  if (op && op.cancelSignal) racers.push(op.cancelSignal);
  return Promise.race(racers).finally(() => clearTimeout(timer));
}

function errorText(err) {
  if (err == null) return "";
  if (typeof err === "string") return err;
  const parts = [];
  if (err.name && err.name !== "Error") parts.push(err.name);
  if (err.code && typeof err.code === "string") parts.push(err.code);
  if (err.status) parts.push("HTTP " + err.status);
  const msg = err.message || err.error || "";
  if (msg) parts.push(typeof msg === "string" ? msg : JSON.stringify(msg));
  if (!msg && typeof err === "object") {
    try {
      parts.push(JSON.stringify(err));
    } catch (_) {
      parts.push(String(err));
    }
  }
  return parts.join(": ");
}

const RE_TIMEOUT = /time(d)?[ -]?out|etimedout|deadline/i;
const RE_PERM = /eacces|eperm|permission|forbidden|read-only|readonly|not writable|enoent|enospc|no such file|no space|disk full|unauthori[sz]ed|\b40[13]\b/i;
const RE_UNREACH = /unreachable|econnrefused|econnreset|enotfound|no route|connection (error|refused|reset|closed|lost)|failed to fetch|load failed|networkerror|offline|no deployments|service unavailable|bad gateway|internalservererror|name or service not known|agent .*not found|service_not_found|\b50[234]\b/i;
const RE_EMPTY = /empty (reply|response)|junk|no (reply|response|speech)/i;
const RE_INVALID = /json|parse|did not return|invalid (reply|response)|unexpected token/i;
const RE_NOCONN = /not connected/i;

/**
 * Map any thrown value (Error, HA websocket {code,message}, backend envelope)
 * to {code, cause, message, detail, retryable}. Detail is redacted.
 */
export function classifyError(err) {
  const text = errorText(err);
  const detail = redactSecrets(text).slice(0, 1200);
  const make = (code, message, retryable) => ({
    code,
    cause: CAUSES[code] || CAUSES.unexpected,
    message,
    detail,
    retryable,
  });
  const name = err && err.name;
  if (name === "OpCancelledError") return make("cancelled", "You cancelled this. Nothing was changed.", false);
  if (name === "OpTimeoutError") return make("ai_timeout", "The AI service took too long to answer.", true);
  if (name === "AgentError") {
    if (err.agentCode === "no_intent_match")
      return make(
        "ai_invalid_reply",
        "The chosen agent only understands Home Assistant commands, not free text. Point agent_id at an LLM agent.",
        false
      );
    return make("ai_unreachable", "The AI service behind the agent failed and Home Assistant relayed its error.", true);
  }
  if (name === "ReplyParseError") {
    const empty = !(err.raw || "").trim();
    return empty
      ? make("ai_empty_reply", "The AI service sent an empty reply.", true)
      : make("ai_invalid_reply", "The AI service answered, but not in a form that could be read as JSON.", true);
  }
  if (err && err.api && CAUSES[err.api.code]) {
    return make(err.api.code, err.api.message || CAUSES[err.api.code], err.api.code !== "file_permission");
  }
  if (RE_NOCONN.test(text)) return make("not_connected", "Home Assistant is not connected. Reload the dashboard and try again.", true);
  if (RE_TIMEOUT.test(text) || (err && err.status === 504)) return make("ai_timeout", "The AI service took too long to answer.", true);
  if (RE_PERM.test(text)) return make("file_permission", "A file could not be read or written (permission or missing path).", false);
  if (RE_UNREACH.test(text)) return make("ai_unreachable", "The AI service could not be reached.", true);
  if (RE_EMPTY.test(text)) return make("ai_empty_reply", "The AI service sent an empty reply.", true);
  if (RE_INVALID.test(text)) return make("ai_invalid_reply", "The AI service sent a reply that could not be read.", true);
  return make("unexpected", "Something unexpected went wrong.", true);
}

/**
 * A long action's visible state. ``title`` is the live status line prefix,
 * ``step`` the current phase, ``done``/``total`` drive the progress bar.
 */
export function createOp(spec, now = new Date()) {
  const op = {
    kind: spec.kind,
    title: spec.title,
    step: spec.step || "working",
    unit: spec.unit || "tracks",
    total: spec.total == null ? null : spec.total,
    done: 0,
    showCounts: false,
    cancellable: !!spec.cancellable,
    doneVerb: spec.doneVerb || "",
    noChangeNote: spec.noChangeNote || "",
    state: "running", // running | cancelling | failed | cancelled
    request_id: newRequestId(),
    startedAt: now.toISOString(),
    error: null,
    retry: spec.retry || null,
    cancelRequested: false,
    cancelSignal: null,
    _cancel: null,
  };
  op.cancelSignal = new Promise((_, reject) => {
    op._cancel = () => reject(new OpCancelledError());
  });
  op.cancelSignal.catch(() => {});
  return op;
}

export function opUpdate(op, patch) {
  Object.assign(op, patch || {});
  return op;
}

/** Ask a running op to stop; the in-flight request is abandoned, not undone. */
export function opCancel(op) {
  if (!op || op.state !== "running" || !op.cancellable) return false;
  op.cancelRequested = true;
  op.state = "cancelling";
  if (op._cancel) op._cancel();
  return true;
}

/** Throw between steps once the user has pressed Cancel. */
export function opCheck(op) {
  if (op && op.cancelRequested) throw new OpCancelledError();
}

export function opPercent(op) {
  if (!op || op.total == null || !(op.total > 0)) return null;
  return Math.max(0, Math.min(100, Math.round((op.done / op.total) * 100)));
}

/** The ONE live status line, e.g. 'Splitting "X" into A + B… analysing 18 of 42 tracks'. */
export function opStatusLine(op) {
  if (!op) return "";
  if (op.state === "cancelling") return op.title + "… cancelling (the request already sent may still finish)";
  let line = op.title + "… " + op.step;
  if (op.showCounts && op.total != null) line += " " + op.done + " of " + op.total + " " + op.unit;
  return line;
}

/** True while a long action is in flight: every other action control is disabled. */
export function controlsLocked(op) {
  return !!op && (op.state === "running" || op.state === "cancelling");
}

/** Move an op to its failed (or cancelled) state with a plain-words explanation. */
export function failOp(op, err, now = new Date()) {
  const c = classifyError(err);
  const cancelled = c.code === "cancelled";
  const completed = op.done || 0;
  let outcome;
  if (op.total != null && op.showCounts) {
    const left = Math.max(0, op.total - completed);
    outcome =
      completed + " of " + op.total + " " + op.unit + " " + (op.doneVerb || "analysed") + ", " + left + " not " + (op.doneVerb || "analysed") + ".";
  } else {
    outcome = "Nothing was completed.";
  }
  if (op.noChangeNote) outcome += " " + op.noChangeNote;
  op.state = cancelled ? "cancelled" : "failed";
  op.error = Object.assign({}, c, {
    request_id: (err && err.api && err.api.request_id) || op.request_id,
    at: now.toISOString(),
    outcome,
    failedStep: op.step,
  });
  return op;
}

/** Plain-text report for the Copy details button (already redacted). */
export function failureDetails(op) {
  const e = (op && op.error) || {};
  return [
    "Action: " + (op ? op.title : "?"),
    "Step: " + (e.failedStep || "?"),
    "Cause: " + (e.cause || "?") + " (" + (e.code || "?") + ")",
    "Message: " + (e.message || ""),
    "Completed: " + (e.outcome || ""),
    "Request id: " + (e.request_id || ""),
    "Time: " + (e.at || ""),
    "Detail: " + (e.detail || ""),
  ].join("\n");
}

/**
 * Home Assistant's conversation.process reports an agent/backend failure as a
 * *successful* call whose response has response_type "error" and an apology
 * in the speech ("Sorry, I had a problem talking to ..."). Detect that so it
 * is reported as the AI service failing, not as "the agent did not return JSON".
 */
export class AgentError extends Error {
  constructor(message, code) {
    super(message || "The conversation agent reported an error");
    this.name = "AgentError";
    this.agentCode = code || "";
  }
}

export function assertAgentOk(result) {
  const roots = [result && result.response, result && result.response && result.response.response, result];
  for (const r of roots) {
    if (r && r.response_type === "error") {
      const code = (r.data && r.data.code) || "";
      throw new AgentError(extractSpeech(result), code);
    }
  }
  return result;
}
