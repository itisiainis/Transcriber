// Side panel. Talks to host.py over one native messaging port while open;
// the host enqueues work and the worker does it, so closing the panel never
// stops a transcription.

const HOST = "com.transcriber.host";
const POLL_ACTIVE = 1500;   // ms, while something is queued or running
const POLL_IDLE = 8000;
const RECONNECT = 5000;
const CALL_TIMEOUT = 15000;

const $ = (id) => document.getElementById(id);

const ICON = {
  check: '<svg viewBox="0 0 16 16"><path d="m3.5 8.5 3 3 6-7"/></svg>',
  chev: '<svg viewBox="0 0 16 16"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>',
  alert: '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="8.2"/><path d="M10 5.8v5"/><circle cx="10" cy="14" r=".4" class="fill" style="stroke:currentColor"/></svg>',
  spin: '<svg viewBox="0 0 22 22"><circle class="track" cx="11" cy="11" r="8.5"/><path class="arc" d="M11 2.5a8.5 8.5 0 0 1 8.5 8.5"/></svg>',
  ok: '<svg viewBox="0 0 16 16"><path d="m3.5 8.5 3 3 6-7"/></svg>',
};

// --- host connection -------------------------------------------------------

const host = {
  port: null,
  seq: 0,
  pending: new Map(),
  error: null,         // why the last connection dropped, if it did

  connect() {
    this.port = chrome.runtime.connectNative(HOST);
    this.port.onMessage.addListener((m) => this.receive(m));
    this.port.onDisconnect.addListener(() => {
      this.error = chrome.runtime.lastError?.message || "The host exited";
      this.port = null;
      for (const p of this.pending.values()) p.reject(new Error(this.error));
      this.pending.clear();
      state.connected = false;
      render();
      // The poll loop reconnects; start it again if nothing is pending.
      clearTimeout(pollTimer);
      pollTimer = setTimeout(refresh, RECONNECT);
    });
  },

  receive(m) {
    const p = this.pending.get(m.id);
    if (!p) return;
    if (!m.ok) {
      this.pending.delete(m.id);
      p.reject(new Error(m.error || "Host error"));
      return;
    }
    // Long transcripts arrive in parts (Chrome caps a message at 1 MB).
    if (m.parts !== undefined) {
      p.parts.push(m);
      if (p.parts.length < m.parts) return;
      this.pending.delete(m.id);
      p.resolve(p.parts.sort((a, b) => a.part - b.part));
      return;
    }
    this.pending.delete(m.id);
    p.resolve(m);
  },

  call(cmd, args = {}) {
    if (!this.port) this.connect();
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("The host didn't answer"));
      }, CALL_TIMEOUT);
      this.pending.set(id, {
        parts: [],
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.port.postMessage({ ...args, id, cmd });   // the request id always wins
    });
  },
};

// --- state -----------------------------------------------------------------

const state = {
  connected: false,
  worker: null,
  rows: [],
  unread: 0,
  page: null,          // { videoId, title, channel } or { channelUrl, title } for the active tab
  selecting: false,    // multi-select mode on the Transcribe list
  selected: new Set(), // video ids ticked in it
  transcript: null,    // the open transcript, if any
  transcriptId: null,  // the video whose transcript screen is showing
  threads: [],         // analyses, one per thread (analyse.js)
  prompts: { items: [], last: null },
  analysisJobs: [],
};

let pollTimer = null;

async function refresh() {
  clearTimeout(pollTimer);
  try {
    const s = await host.call("state");
    Object.assign(state, {
      connected: true, worker: s.worker, rows: s.rows, unread: s.unread,
      threads: s.threads, prompts: s.prompts, analysisJobs: s.analysis_jobs,
    });
    host.error = null;
  } catch (e) {
    state.connected = false;
    if (!host.error) host.error = e.message;
  }
  render();
  clearTimeout(pollTimer);
  const busy = state.rows.some((r) => r.state === "running" || r.state === "queued")
    || state.analysisJobs.some((j) => j.status === "running" || j.status === "queued");
  pollTimer = setTimeout(refresh, !state.connected ? RECONNECT : busy ? POLL_ACTIVE : POLL_IDLE);
}

function rowFor(videoId) {
  return state.rows.find((r) => r.video_id === videoId);
}

// --- the video on the current tab -----------------------------------------

// Mirrors transcribe.parse_url(): YouTube ids are 11 characters, RuTube's
// 32 hex, so an id alone says which site it is.
function videoIdFrom(url) {
  let u;
  try { u = new URL(url); } catch { return null; }
  if (/(^|\.)rutube\.ru$/.test(u.hostname)) {
    const m = u.pathname.match(/^\/(?:video|shorts|play\/embed)\/(?:private\/)?([0-9a-f]{32})/);
    return m ? m[1] : null;
  }
  const id11 = (s) => (s && /^[\w-]{11}$/.test(s) ? s : null);
  if (u.hostname === "youtu.be") return id11(u.pathname.slice(1, 12));
  if (!/(^|\.)youtube\.com$/.test(u.hostname)) return null;
  if (u.pathname === "/watch") return id11(u.searchParams.get("v"));
  const m = u.pathname.match(/^\/(?:shorts|live|embed|v)\/([\w-]{11})/);
  return m ? m[1] : null;
}

// Mirrors channel.parse_channel(): a channel page, whose videos can be listed.
function channelFrom(url) {
  let u;
  try { u = new URL(url); } catch { return null; }
  if (/(^|\.)rutube\.ru$/.test(u.hostname)) {
    return /^\/(channel\/\d+|u\/\w+)/.test(u.pathname) ? url : null;
  }
  if (!/(^|\.)youtube\.com$/.test(u.hostname)) return null;
  return /^\/(@[^/]+|channel\/UC[\w-]{22}|c\/[^/]+|user\/[^/]+)/.test(u.pathname) ? url : null;
}

function videoUrl(videoId, t) {
  if (/^[0-9a-f]{32}$/.test(videoId)) return `https://rutube.ru/video/${videoId}/` + (t ? `?t=${t}` : "");
  return `https://www.youtube.com/watch?v=${videoId}` + (t ? `&t=${t}s` : "");
}

// Runs inside the YouTube page. Tab titles lag behind YouTube's in-page
// navigation, so the rendered heading and channel link are read directly.
function readPageVideo() {
  const text = (sel) => document.querySelector(sel)?.textContent?.trim() || null;
  return {
    title: text("ytd-watch-metadata h1 yt-formatted-string") || text("h1.ytd-watch-metadata"),
    channel: text("ytd-watch-metadata #owner #channel-name a") || text("#owner #channel-name a")
      || text("ytd-reel-player-overlay-renderer .ytd-channel-name a"),
  };
}

let windowId = null;

async function detectPage() {
  const [tab] = await chrome.tabs.query({ active: true, windowId: windowId ?? chrome.windows.WINDOW_ID_CURRENT });
  const videoId = tab && videoIdFrom(tab.url || "");
  // Fall back to the tab title ("(3) Title - YouTube") until the page answers.
  // RuTube's page has no YouTube markup, so its title is all there is.
  const fallback = (tab?.title || "").replace(/^\(\d+\)\s*/, "").replace(/\s*-\s*YouTube$/, "")
    .replace(/\s+[—–-]\s+смотреть.*$/i, "").replace(/\s*[|—–-]\s*RUTUBE$/i, "");
  if (!videoId) {
    const channelUrl = tab && channelFrom(tab.url || "");
    state.page = channelUrl ? { channelUrl, title: fallback || null } : null;
    render();
    return;
  }
  let info = {};
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: readPageVideo });
    info = res?.result || {};
  } catch { /* page still loading, or not scriptable */ }
  state.page = { videoId, title: info.title || fallback || null, channel: info.channel || null };
  render();
}

// --- actions ---------------------------------------------------------------

async function submit(url, entry) {
  const known = rowFor(videoIdFrom(url) || url.trim());
  if (known && known.state === "done" && known.has_transcript) {
    openTranscript(known.video_id);
    return true;
  }
  try {
    await host.call("transcribe", { url, entry });
  } catch (e) {
    showLinkError(e.message);
    render();
    return false;
  }
  refresh();
  return true;
}

async function retry(videoId) {
  try { await host.call("retry", { video_id: videoId }); } catch (e) { showLinkError(e.message); }
  refresh();
}

function showLinkError(msg) {
  const el = $("link-error");
  el.textContent = msg;
  el.hidden = !msg;
}

function setTab(tab) {
  $("panel").dataset.tab = tab;
  for (const b of document.querySelectorAll(".tab")) {
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  }
  $("view-transcribe").hidden = tab !== "transcribe";
  $("view-analyse").hidden = tab !== "analyse";
}

// --- rendering: header and intake -----------------------------------------

function fmtDuration(s) {
  if (!s && s !== 0) return null;
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

// "~4h 20m" — the same rounding as batch.fmt_estimate().
function fmtEstimate(s) {
  if (s == null) return null;
  const m = Math.round(s / 60);
  if (m < 1) return "<1m";
  return m >= 60 ? `~${Math.floor(m / 60)}h ${m % 60}m` : `~${m}m`;
}

function render() {
  renderIntake();
  renderWorker();
  renderSelectBar();
  renderRows();
  if (typeof renderChannel === "function") renderChannel();
  const badge = $("unread");
  badge.textContent = state.unread;
  badge.hidden = !state.unread;
  renderAnalyse();
}

function renderIntake() {
  const btn = $("page-btn"), label = $("page-btn-label"), text = $("page-video-text");
  const p = state.page;
  if (!p) {
    btn.disabled = true;
    label.textContent = "Transcribe video on page";
    text.textContent = "Open a YouTube or RuTube video to transcribe it from here";
    return;
  }
  if (p.channelUrl) {
    btn.disabled = !state.connected;
    label.textContent = "Show this channel's videos";
    text.textContent = p.title || "Channel";
    return;
  }
  text.textContent = [p.title || p.videoId, p.channel].filter(Boolean).join(" — ");
  const row = rowFor(p.videoId);
  if (row && (row.state === "running" || row.state === "queued")) {
    btn.disabled = true;
    label.textContent = row.state === "running" ? "Transcribing…" : "Queued";
  } else if (row && row.state === "done" && row.has_transcript) {
    btn.disabled = false;
    label.textContent = "Open transcript";
  } else {
    btn.disabled = !state.connected;
    label.textContent = "Transcribe video on page";
  }
}

function renderWorker() {
  const w = $("worker"), text = $("worker-text"), help = $("host-help");
  const count = state.rows.length;
  $("count").textContent = state.connected ? `${count} video${count === 1 ? "" : "s"}` : "";
  if (!state.connected) {
    w.dataset.state = "down";
    text.textContent = "Host not connected";
    help.hidden = false;
    help.innerHTML = "";
    help.append(
      document.createTextNode((host.error || "Can't reach the native host") + ". Register it with "),
      Object.assign(document.createElement("code"), { textContent: "host\\install.ps1" }),
      document.createTextNode(" — retrying every few seconds."),
    );
    return;
  }
  help.hidden = true;
  const info = `${state.worker.model} · ${state.worker.threads} threads`;
  if (state.worker.busy) {
    w.dataset.state = "busy";
    text.textContent = `Worker busy · ${info}`;
  } else {
    // An idle worker exits; the host starts one on demand, so the
    // tool is ready whenever the host answers.
    w.dataset.state = "online";
    text.textContent = `Worker online · ${info}`;
  }
}

// --- rendering: the list ---------------------------------------------------

const STAGE_LABEL = {
  queued: "starting",
  metadata: "fetching video info",
  captions: "checking captions",
  download: "downloading audio",
  whisper: "whisper",
  saving: "saving",
};

// Remaining time and bar fraction for a running job, from the host's
// estimates and the stage's start time. null remaining = not yet knowable.
function estimate(p) {
  const now = Date.now() / 1000;
  if (p.after_estimate == null || p.stage_estimate == null) return { left: null, frac: null };
  const inStage = now - (p.stage_started || now);
  const left = Math.max(p.stage_estimate - inStage, 0) + p.after_estimate;
  const elapsed = now - (p.started || now);
  const over = inStage > p.stage_estimate * 1.2 + 10;
  return { left, over, frac: Math.min(elapsed / Math.max(elapsed + left, 1), 0.98) };
}

function progressText(p) {
  const stage = STAGE_LABEL[p.stage] || p.stage;
  const { left, over } = estimate(p);
  if (left == null) return stage;
  if (over) return `${stage} · taking longer than usual`;
  if (left < 60) return `${stage} · under a minute left`;
  return `${stage} · about ${Math.round(left / 60)} min left`;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// --- multi-select ------------------------------------------------------------
//
// Entered from the Select button or a right-click on a row (never a
// long-press: that is a touch idiom and fights text selection). Covers
// export and delete alike. Rows still queued or running can't be ticked.

const selectable = (r) => r.state !== "running" && r.state !== "queued";
const selectedRows = () => state.rows.filter((r) => state.selected.has(r.video_id));
// Oldest first in an export: the order they were transcribed.
const selectedExportable = () => selectedRows().filter((r) => r.has_transcript).reverse();

function startSelect(videoId) {
  state.selecting = true;
  if (videoId) state.selected.add(videoId);
  render();
}

function stopSelect() {
  state.selecting = false;
  state.selected.clear();
  showSelError("");
  disarm($("sel-delete"));
  render();
}

function toggleSelect(videoId) {
  if (state.selected.has(videoId)) state.selected.delete(videoId);
  else state.selected.add(videoId);
  disarm($("sel-delete"));
  render();
}

function showSelError(msg) {
  $("sel-error").textContent = msg;
  $("sel-error").hidden = !msg;
}

function renderSelectBar() {
  $("select-btn").hidden = state.selecting || !state.connected || !state.rows.length;
  $("select-bar").hidden = !state.selecting;
  if (!state.selecting) return;
  for (const id of state.selected) if (!rowFor(id)) state.selected.delete(id);
  const n = state.selected.size, exportable = selectedExportable().length;
  $("select-count").textContent = n ? `${n} selected` : "Tick videos to export or delete";
  for (const id of ["sel-export", "sel-export-zip"]) {
    $(id).disabled = !exportable;
    $(id).title = exportable < n ? `${n - exportable} without a transcript will be left out` : "";
  }
  $("sel-delete").disabled = !n;
}

function saveFile(blob, filename) {
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob),
    download: filename.replace(/[\\/:*?"<>|]+/g, " ").trim(),
  });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// export.py builds the file; it arrives as base64 in parts (1 MB message cap).
async function exportSelected(mode, btn) {
  const ids = selectedExportable().map((r) => r.video_id);
  if (!ids.length) return;
  showSelError("");
  btn.disabled = true;
  try {
    const parts = await host.call("export", { video_ids: ids, mode });
    const list = Array.isArray(parts) ? parts : [parts];
    const bin = atob(list.map((p) => p.data).join(""));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    saveFile(new Blob([bytes], { type: list[0].mime }), list[0].filename);
    flash(btn, "Saved");
  } catch (e) {
    showSelError(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function deleteSelected() {
  const failed = new Map();     // video id -> why; those stay ticked
  for (const r of selectedRows()) {
    try { await host.call("delete_video", { video_id: r.video_id }); }
    catch (e) { failed.set(r.video_id, `${r.title || r.video_id}: ${e.message}`); }
  }
  if (failed.size) {
    state.selected = new Set(failed.keys());
    showSelError([...failed.values()].join("\n"));
  } else {
    stopSelect();
  }
  refresh();
}

function buildSelectRow(li, r) {
  const ok = selectable(r);
  const box = el("input", "pick-box");
  box.type = "checkbox";
  box.tabIndex = -1;
  box.checked = state.selected.has(r.video_id);
  box.disabled = !ok;
  const body = el("div", "body");
  body.append(el("div", "title", r.title || r.video_id),
    el("div", "sub", [r.channel, fmtDuration(r.duration),
      r.has_transcript ? r.source : r.state === "failed" ? "failed" : r.state].filter(Boolean).join(" · ")));
  li.append(box, body, el("span"));
  li.classList.add("picking");
  li.classList.toggle("picked", box.checked);
  if (ok) {
    li.tabIndex = 0;
    li.addEventListener("click", () => toggleSelect(r.video_id));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleSelect(r.video_id); }
    });
  }
  return li;
}

function buildRow(r) {
  const li = el("li", `row ${r.state}`);
  li.dataset.id = r.video_id;
  li.addEventListener("contextmenu", (e) => {
    if (!selectable(r)) return;
    e.preventDefault();
    if (state.selecting) toggleSelect(r.video_id);
    else startSelect(r.video_id);
  });
  if (state.selecting) return buildSelectRow(li, r);

  const glyph = el("span", "glyph");
  const body = el("div", "body");
  const title = el("div", "title", r.title || (r.state === "failed" ? r.video_id : "Fetching video info…"));
  if (!r.title) title.classList.add("pending");
  title.title = r.title || r.video_id;
  const sub = el("div", "sub");
  body.append(title, sub);
  li.append(glyph, body);

  if (r.state === "running") {
    glyph.classList.add("spin");
    glyph.innerHTML = ICON.spin;
    sub.classList.add("progress-text");
    li.append(el("span"));
    const bar = el("div", "bar");
    bar.append(el("i"));
    li.append(bar);
  } else if (r.state === "queued") {
    glyph.classList.add("plain");
    sub.textContent = [r.queue_position > 1 ? `queued · ${r.queue_position} in line` : "queued · next up",
      fmtEstimate(r.estimate)].filter(Boolean).join(" · ");
    li.append(el("span"));
  } else if (r.state === "failed") {
    glyph.classList.add("bad");
    glyph.innerHTML = ICON.alert;
    sub.textContent = r.error;
    sub.title = r.error_full || r.error;
    const btn = el("button", "retry", "Retry");
    btn.addEventListener("click", (e) => { e.stopPropagation(); btn.disabled = true; retry(r.video_id); });
    const remove = el("button", "retry quiet", "Remove");
    remove.title = "Hide this video; its log rows stay for the stats";
    confirmTwice(remove, "Sure?", () => deleteVideo(r.video_id));
    const actions = el("div", "actions");
    actions.append(btn, remove);
    li.append(actions);
  } else {
    // Orange check = an analysis exists; a dashed ring = not analysed yet.
    if (r.analysed) { glyph.classList.add("analysed"); glyph.innerHTML = ICON.check; }
    else glyph.classList.add("plain");
    sub.textContent = [r.channel, fmtDuration(r.duration), r.source].filter(Boolean).join(" · ");
    const chev = el("span", "chev");
    if (r.has_transcript) {
      chev.innerHTML = ICON.chev;
      li.classList.add("openable");
      li.tabIndex = 0;
      li.addEventListener("click", () => openTranscript(r.video_id));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") openTranscript(r.video_id); });
    }
    li.append(chev);
  }
  return li;
}

// Rows are rebuilt only when their data changes, so spinners don't restart
// on every poll. The running row's countdown is updated by tick().
const rowCache = new Map();   // video_id -> { sig, node }

function renderRows() {
  const list = $("rows");
  $("empty").hidden = !state.connected || state.rows.length > 0;
  const seen = new Set();
  let prev = null;
  for (const r of state.rows) {
    seen.add(r.video_id);
    const { progress, ...stable } = r;
    const sig = JSON.stringify({ ...stable, stage: progress?.stage,
      selecting: state.selecting, picked: state.selected.has(r.video_id) });
    let cached = rowCache.get(r.video_id);
    if (!cached || cached.sig !== sig) {
      const node = buildRow(r);
      if (cached) cached.node.replaceWith(node);
      cached = { sig, node };
      rowCache.set(r.video_id, cached);
    }
    cached.progress = progress;
    const want = prev ? prev.nextSibling : list.firstChild;
    if (want !== cached.node) list.insertBefore(cached.node, want);
    prev = cached.node;
  }
  for (const [id, c] of rowCache) {
    if (!seen.has(id)) { c.node.remove(); rowCache.delete(id); }
  }
  tick();
}

function tick() {
  for (const c of rowCache.values()) {
    if (!c.progress || state.selecting) continue;
    c.node.querySelector(".progress-text").textContent = progressText(c.progress);
    const bar = c.node.querySelector(".bar");
    const { frac } = estimate(c.progress);
    bar.classList.toggle("unknown", frac == null);
    bar.firstChild.style.width = frac == null ? "" : `${(frac * 100).toFixed(1)}%`;
  }
  tickAnalyse();
}

// --- screens ---------------------------------------------------------------

// Screens slide in over the panel and over each other; the newest is on
// top, and Escape or its back button closes that one.
const screenStack = [];

function openScreen(id) {
  const s = $(id);
  const at = screenStack.indexOf(id);
  if (at !== -1) screenStack.splice(at, 1);
  screenStack.push(id);
  s.style.zIndex = String(10 + screenStack.length);
  s.classList.add("open");
  s.setAttribute("aria-hidden", "false");
}

function closeScreen(id) {
  const at = screenStack.indexOf(id);
  if (at !== -1) screenStack.splice(at, 1);
  const s = $(id);
  s.classList.remove("open");
  s.setAttribute("aria-hidden", "true");
  onScreenClosed(id);
}

function closeAllScreens() {
  for (const id of [...screenStack]) closeScreen(id);
}

// --- transcript screen -----------------------------------------------------

function ts(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// Caption segments are a line or two each; read as paragraphs instead.
// Break at a sentence end once a paragraph is long enough, or regardless
// once it is too long (auto-captions have no punctuation to break on).
function paragraphs(segments) {
  const out = [];
  let cur = null;
  for (const [start, raw] of segments) {
    const text = raw.replace(/\s+/g, " ").trim();
    if (!text) continue;
    if (!cur) { cur = { start, parts: [] }; out.push(cur); }
    cur.parts.push(text);
    const span = start - cur.start;
    if ((span >= 12 && /[.!?…]["»”)]?$/.test(text)) || span >= 28) cur = null;
  }
  return out.map((p) => ({ start: p.start, text: p.parts.join(" ") }));
}

async function openTranscript(videoId) {
  const screen = $("transcript"), body = $("tr-body");
  const row = rowFor(videoId);
  $("tr-title").textContent = row?.title || videoId;
  $("tr-meta").textContent = [row?.channel, fmtDuration(row?.duration)].filter(Boolean).join(" · ");
  $("tr-source").textContent = row?.source || "";
  body.replaceChildren(el("p", "loading", "Loading transcript…"));
  body.scrollTop = 0;
  $("tr-error").hidden = true;
  disarm($("tr-delete"));
  openScreen("transcript");
  state.transcript = null;
  state.transcriptId = videoId;

  let parts;
  try {
    parts = await host.call("transcript", { video_id: videoId });
  } catch (e) {
    body.replaceChildren(el("p", "loading", e.message));
    return;
  }
  const head = parts[0];
  const segments = parts.flatMap((p) => p.segments);
  state.transcript = { ...head, segments };
  $("tr-title").textContent = head.title || videoId;
  $("tr-meta").textContent = [head.channel, fmtDuration(head.duration)].filter(Boolean).join(" · ");
  $("tr-source").textContent = head.source_label || "";

  const frag = document.createDocumentFragment();
  for (const p of paragraphs(segments)) {
    const block = el("div", "para");
    block.append(el("time", null, ts(p.start)), el("p", null, p.text));
    frag.append(block);
  }
  body.replaceChildren(frag);
}

function closeTranscript() {
  closeScreen("transcript");
}

function transcriptText() {
  const t = state.transcript;
  if (!t) return "";
  const head = [t.title, [t.channel, fmtDuration(t.duration)].filter(Boolean).join(" · "), ""];
  return head.concat(paragraphs(t.segments).map((p) => `[${ts(p.start)}] ${p.text}`)).join("\n");
}

function flash(btn, label = "Done") {
  const before = btn.innerHTML;
  // Toolbar buttons carry a label; say what happened rather than just tick.
  btn.innerHTML = ICON.ok + (btn.classList.contains("tool") ? `<span>${label}</span>` : "");
  btn.classList.add("done");
  setTimeout(() => { btn.innerHTML = before; btn.classList.remove("done"); }, 1200);
}

// Destructive buttons take two presses: the first turns the label into a
// question, the second acts. Nothing in the panel opens a dialog.
function confirmTwice(btn, question, action) {
  const label = () => btn.querySelector("span") || btn;
  let timer = null;
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!btn.dataset.armed) {
      btn.dataset.armed = "1";
      btn.dataset.before = label().textContent;
      label().textContent = typeof question === "function" ? await question() : question;
      btn.classList.add("armed");
      timer = setTimeout(() => disarm(btn), 4000);
      return;
    }
    clearTimeout(timer);
    disarm(btn);
    btn.disabled = true;
    try { await action(); } finally { btn.disabled = false; }
  });
}

function disarm(btn) {
  if (!btn.dataset.armed) return;
  (btn.querySelector("span") || btn).textContent = btn.dataset.before;
  delete btn.dataset.armed;
  btn.classList.remove("armed");
}

// Soft delete (store.soft_delete_video): the transcript and analysis files go,
// the rows stay for the stats. Submitting the video again brings it back.
async function deleteVideo(videoId) {
  try {
    await host.call("delete_video", { video_id: videoId });
  } catch (e) {
    const err = $("tr-error");
    if (screenStack.includes("transcript")) { err.textContent = e.message; err.hidden = false; }
    else showLinkError(e.message);
    return;
  }
  if (state.transcriptId === videoId) closeScreen("transcript");
  if (ui.thread?.video_id === videoId) closeScreen("analysis");
  refresh();
}

// --- wiring ----------------------------------------------------------------

for (const b of document.querySelectorAll(".tab")) {
  b.addEventListener("click", () => setTab(b.dataset.tab));
}

$("page-btn").addEventListener("click", async () => {
  const p = state.page;
  if (!p) return;
  if (p.channelUrl) return openChannel(p.channelUrl, p.title);
  const row = rowFor(p.videoId);
  if (row && row.state === "done" && row.has_transcript) return openTranscript(p.videoId);
  $("page-btn").disabled = true;
  await submit(videoUrl(p.videoId), "page");
});

$("link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("link-input");
  const url = input.value.trim();
  if (!url) return;
  // Which sites work is the host's call (transcribe.parse_url); its error
  // names the site that isn't supported.
  showLinkError("");
  if (channelFrom(url) && !videoIdFrom(url)) {
    input.value = "";
    return openChannel(url);
  }
  if (await submit(url, "link")) input.value = "";
});
$("link-input").addEventListener("input", () => showLinkError(""));

$("tr-back").addEventListener("click", closeTranscript);
$("tr-copy").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  await navigator.clipboard.writeText(transcriptText());
  flash(btn, "Copied");
});
// Deleting a video takes its analyses with it (their files live in its folder).
confirmTwice($("tr-delete"),
  () => (rowFor(state.transcriptId)?.analysed ? "Delete + analyses?" : "Delete — sure?"),
  () => deleteVideo(state.transcriptId));
$("settings-open").addEventListener("click", () => openSettings());
$("tr-download").addEventListener("click", (e) => {
  const t = state.transcript;
  if (!t) return;
  saveFile(new Blob([transcriptText()], { type: "text/plain;charset=utf-8" }), `${t.title || t.video_id}.txt`);
  flash(e.currentTarget, "Saved");
});

$("select-btn").addEventListener("click", () => startSelect());
$("sel-done").addEventListener("click", stopSelect);
$("sel-export").addEventListener("click", (e) => exportSelected("combined", e.currentTarget));
$("sel-export-zip").addEventListener("click", (e) => exportSelected("separate", e.currentTarget));
confirmTwice($("sel-delete"), () => `Delete ${state.selected.size}?`, deleteSelected);
// The one clay control on a blue screen: it hands the video to Analyse.
$("tr-analyse").addEventListener("click", () => startAnalyse(state.transcriptId));

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (screenStack.length) closeScreen(screenStack.at(-1));
  else if (state.selecting) stopSelect();
});

chrome.tabs.onActivated.addListener((info) => { if (info.windowId === windowId) detectPage(); });
chrome.tabs.onUpdated.addListener((_id, change, tab) => {
  if (tab.active && tab.windowId === windowId && (change.url || change.title || change.status === "complete")) {
    detectPage();
  }
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(tick, 1000);

// After every script has run: rendering reaches into analyse.js, and a
// microtask here would otherwise run before that file has loaded.
document.addEventListener("DOMContentLoaded", async () => {
  windowId = (await chrome.windows.getCurrent()).id;
  detectPage();
  refresh();
});
