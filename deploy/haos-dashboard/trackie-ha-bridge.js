/** HA-only bridge: browse a read-only CT107 index, resolve audio through HA. */
export function installHaLibraryBridge(Card) {
  const originalToggle = Card.prototype._togglePlay;
  const originalRender = Card.prototype._render;
  const originalHass = Object.getOwnPropertyDescriptor(Card.prototype, "hass");
  const originalDisconnect = Card.prototype.disconnectedCallback;
  Object.defineProperty(Card.prototype, "_readOnly", { get: () => true });
  Object.defineProperty(Card.prototype, "hass", {
    get() { return originalHass.get.call(this); },
    set(value) {
      originalHass.set.call(this, value);
      const status = value && value.states && value.states["sensor.trackie_sync_status"];
      if (status !== this._haLastStatus) {
        this._haLastStatus = status;
        this._renderHaSyncStatus();
      }
    },
  });
  Card.prototype._renderHaSyncStatus = function () {
    const root = this.shadowRoot;
    if (!root) return;
    let panel = root.querySelector("#ct107-sync-status");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "ct107-sync-status";
      panel.className = "note";
      panel.style.overflowWrap = "anywhere";
      panel.setAttribute("aria-label", "Trackie sync progress");
      const head = root.querySelector(".hd");
      if (!head) return;
      head.after(panel);
    }
    panel.replaceChildren();
    const heading = document.createElement("h3");
    heading.textContent = "Sync progress";
    panel.append(heading);
    const entity = this._hass && this._hass.states &&
      this._hass.states["sensor.trackie_sync_status"];
    const a = entity && entity.attributes || {};
    const phase = entity && entity.state || "offline";
    const labels = {starting:"Preparing sync", reading_playlists:"Checking selected playlists",
      checking:"Checking the next track", waiting:"Waiting at playback speed",
      downloading:"Downloading audio and artwork", complete:"Sync complete",
      partial:"Finished with tracks needing attention", stopped:"Sync stopped",
      offline:"Sync status unavailable", unavailable:"Music share unavailable"};
    const message = document.createElement("p");
    message.setAttribute("role", "status");
    message.setAttribute("aria-live", "polite");
    const next = Date.parse(a.next_transfer_at || "");
    const updated = Date.parse(a.updated_at || "");
    const active = ["starting","reading_playlists","checking","waiting","downloading"].includes(phase);
    const stale = active && (!Number.isFinite(updated) ||
      Date.now() > Math.max(updated + 180000, (next || 0) + 180000));
    message.textContent = stale ? "Progress is out of date; the run outcome is unconfirmed." :
      (labels[phase] || "Sync status unavailable");
    panel.append(message);
    if (a.selected_playlists && Array.isArray(a.selected_playlists)) {
      const selected = document.createElement("p");
      selected.textContent = "Selected: " + a.selected_playlists.join(" · ");
      panel.append(selected);
    }
    if (Number.isInteger(a.missing)) {
      const counts = document.createElement("p");
      counts.textContent = (a.already_local || 0) + " already downloaded · " +
        (a.downloaded || 0) + " added this run · " + (a.remaining || 0) +
        " remaining · " + (a.failed || 0) + " need attention";
      panel.append(counts);
      const progress = document.createElement("progress");
      progress.max = Math.max(1, a.missing);
      progress.value = (a.downloaded || 0) + (a.failed || 0);
      progress.setAttribute("aria-label", "Missing tracks processed");
      progress.style.width = "100%";
      panel.append(progress);
    }
    const capacity = a.capacity || {}, queue = capacity.queue, storage = capacity.storage;
    const bytes = value => {
      if (!Number.isFinite(value) || value < 0) return "unknown";
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let i = 0;
      while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
      return value.toLocaleString(undefined, {maximumFractionDigits:1}) + " " + units[i];
    };
    const detail = text => {
      const p = document.createElement("p"); p.textContent = text; panel.append(p);
    };
    if (queue && Number.isInteger(queue.count) && queue.count >= 0) {
      detail((stale || !active ? "Last reported queue: " : "Queued: ") + queue.count + " tracks");
      detail("Estimated saved size: " + bytes(queue.estimated_bytes_min) + "–" +
        bytes(queue.estimated_bytes_max));
      if (queue.duration_unknown)
        detail(queue.duration_unknown + " tracks have unknown length; assuming 4 minutes each.");
      detail("Audio and artwork estimate. Temporary video transfers and working space are additional; network transfer size is unknown.");
    } else detail("Size estimate unavailable for this run.");
    if (storage && storage.available) {
      detail((stale || !active ? "Last reported destination free: " : "Destination free: ") +
        bytes(storage.free_bytes) + " of " + bytes(storage.total_bytes));
      if (queue && queue.estimated_bytes_max > storage.free_bytes)
        detail("Low space: the estimated queue may exceed available storage.");
    } else detail("Destination free space unavailable.");
    if (a.current_title) {
      const track = document.createElement("p");
      track.textContent = "Next track: " + a.current_title;
      panel.append(track);
    }
    if (phase === "waiting" && Number.isFinite(next) && next > Date.now()) {
      const wait = document.createElement("p");
      wait.textContent = "Next transfer after " +
        new Date(next).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}) +
        ". Downloads are spaced to match playback duration.";
      panel.append(wait);
    }
    const note = document.createElement("p");
    note.textContent = a.message || "CT107 runs the sync. This card monitors its progress.";
    panel.append(note);
    if (Number.isFinite(updated)) {
      const time = document.createElement("small");
      time.textContent = "Last update: " + new Date(updated).toLocaleString();
      panel.append(time);
    }
  };
  Card.prototype._render = function () {
    originalRender.call(this);
    const head = this.shadowRoot && this.shadowRoot.querySelector(".hd");
    if (!head || !this._haSnapshotAt) return;
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = "CT107 library snapshot: " + new Date(this._haSnapshotAt).toLocaleString() +
      ". Play previews on this device. ";
    const link = document.createElement("a");
    const music = new URL("/", location.href);
    music.protocol = "http:";
    music.port = "8095";
    link.href = this._config.music_assistant_url || music.href;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open Music Assistant";
    note.append(link);
    head.after(note);
    this._renderHaSyncStatus();
  };
  Card.prototype._loadData = async function () {
    const generation = (this._haLoadGeneration || 0) + 1;
    this._haLoadGeneration = generation;
    this._loading = true;
    this._loadError = "";
    try {
      const response = await fetch("/local/suno-genres/ct107-library.json", { cache: "no-store" });
      if (!response.ok) throw new Error("Library index HTTP " + response.status);
      const body = await response.json();
      if (body.schema !== 1 || !Array.isArray(body.clips) || !body.tracks || !body.genres)
        throw new Error("Invalid CT107 library index");
      if (this._haLoadGeneration !== generation) return;
      this._haSnapshotAt = body.generated_at;
      this._clips = body.clips;
      this._genres = body.genres;
      this._ensure();
      this._tracks = body.tracks;
      this._lyricsCache = body.lyrics || {};
      this._tracksInfo = { state: "ok", reason: "" };
      this._haResolvedAudio = {};
      this._config = Object.assign({}, this._config, {
        webui_url: "", api_url: "", tracks_url: "", lyrics_url: "",
        apply_url: "", undo_url: "", reclassify_url: "", selection_url: "",
      });
    } catch (e) {
      if (this._haLoadGeneration !== generation) return;
      this._loadError = "Could not load the CT107 library index: " + (e.message || String(e));
      this._tracksInfo = { state: "off", reason: this._loadError };
    }
    if (this._haLoadGeneration !== generation) return;
    this._loading = false;
    this._render();
  };
  // No lookup against the retired backend endpoints, including lyrics warming.
  Card.prototype._fetchLyrics = async function () {};
  Card.prototype._audioUrl = function (id) {
    return (this._haResolvedAudio || {})[id] || "";
  };
  Card.prototype._togglePlay = async function (id) {
    const generation = (this._haPlayGeneration || 0) + 1;
    this._haPlayGeneration = generation;
    if (this._playing === id) return originalToggle.call(this, id);
    const uri = (this._tracks[id] || {}).audio;
    try {
      if (!this._hass || typeof this._hass.callWS !== "function")
        throw new Error("Home Assistant is reconnecting; try again");
      const prefix = "media-source://media_source/local/trackie_ct107/";
      if (typeof uri !== "string" || !uri.startsWith(prefix))
        throw new Error("Track is outside the CT107 media share");
      const path = decodeURIComponent(uri.slice(prefix.length));
      if (path.split("/").some(p => !p || p === "." || p === "..") ||
          path.includes(String.fromCharCode(92)) || path.includes(String.fromCharCode(0)))
        throw new Error("Invalid CT107 track path");
      const result = await this._hass.callWS({
        type: "media_source/resolve_media", media_content_id: uri,
      });
      if (this._haPlayGeneration !== generation || !this.isConnected) return;
      const url = new URL(result.url, location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith("/media/"))
        throw new Error("Home Assistant returned an unexpected media URL");
      this._haResolvedAudio = { [id]: url.href };
      this._err = "";
      delete this._playErr[id];
      originalToggle.call(this, id);
    } catch (e) {
      if (this._haPlayGeneration !== generation) return;
      this._err = "Could not open this track: " + (e.message || String(e));
      this._render();
    }
  };
  Card.prototype.disconnectedCallback = function () {
    this._haPlayGeneration = (this._haPlayGeneration || 0) + 1;
    this._haLoadGeneration = (this._haLoadGeneration || 0) + 1;
    return originalDisconnect.call(this);
  };
}
