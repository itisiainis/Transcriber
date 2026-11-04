// The toolbar button opens the side panel. The panel does everything else,
// except one thing it can't: an analysis takes minutes, and the panel may
// be closed when it finishes. So this worker announces results.
//
// It polls the host only while analyses are pending — the panel sends
// {type: "watch"} after queuing one. An open native port keeps the service
// worker alive, and the port is closed as soon as nothing is pending.

const HOST = "com.transcriber.host";
const POLL = 5000;

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  watch();
});
chrome.runtime.onStartup.addListener(watch);
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "watch") watch();
});

let port = null;
let timer = null;
let seq = 0;

function watch() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch {
    port = null;
    return;
  }
  port.onMessage.addListener(onReply);
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    port = null;
    clearTimeout(timer);
  });
  poll();
}

function poll() {
  port?.postMessage({ id: ++seq, cmd: "watch" });
}

function onReply(m) {
  if (!m.ok) return stop();
  for (const f of m.finished || []) announce(f);
  if (m.active > 0) timer = setTimeout(poll, POLL);
  else stop();
}

function stop() {
  clearTimeout(timer);
  port?.disconnect();
  port = null;
}

function announce(f) {
  const what = f.kind === "follow-up" ? "Follow-up" : f.prompt === "custom" ? "Your question" : f.prompt;
  const ok = f.status === "done";
  chrome.notifications.create(`job-${f.job_id}`, {
    type: "basic",
    iconUrl: "icons/icon128.png",
    title: ok ? `${what} is ready` : `${what} failed`,
    message: f.title || f.video_id,
    contextMessage: ok ? "Open the Transcriber panel to read it" : (f.error || ""),
  });
  if (ok && f.root_id != null) {
    chrome.storage.session.set({ [`notify-job-${f.job_id}`]: f.root_id });
  }
}

// Clicking a notification opens the analysis: the panel picks up
// "openThread" from session storage, whether it is open already or opens now.
chrome.notifications.onClicked.addListener(async (id) => {
  // sidePanel.open() needs a user gesture, and Chrome may not count this
  // click as one — if it refuses, the thread still opens the next time the
  // panel is opened by hand, because openThread stays in session storage.
  chrome.windows.getLastFocused((win) => {
    if (win) chrome.sidePanel.open({ windowId: win.id }).catch(() => {});
  });
  const key = `notify-${id}`;
  const stored = await chrome.storage.session.get(key);
  chrome.notifications.clear(id);
  if (stored[key] != null) {
    await chrome.storage.session.remove(key);
    await chrome.storage.session.set({ openThread: stored[key] });
  }
});

// --- recording ---------------------------------------------------------------
//
// The panel starts and stops; the page (record-probe.js) records; this sits
// between them and the host, because it is the one part that outlives both:
// closing the panel must not stop a two-hour live recording. It holds its own
// native port for the duration — an open port also keeps this service worker
// alive, which a recording that sends a chunk every 10 s needs.
//
// One recording at a time: one CPU, one whisper, and one badge to show it.

let rec = null;          // the current or last recording, as the panel shows it
let recPort = null;
let recSeq = 0;
const recPending = new Map();
let badgeTimer = null;
let recStarting = false;

function hostCall(cmd, args = {}) {
  if (!recPort) {
    recPort = chrome.runtime.connectNative(HOST);
    recPort.onMessage.addListener((m) => {
      const p = recPending.get(m.id);
      if (!p) return;
      recPending.delete(m.id);
      m.ok ? p.resolve(m) : p.reject(new Error(m.error || "Host error"));
    });
    recPort.onDisconnect.addListener(() => {
      const err = new Error(chrome.runtime.lastError?.message || "The host exited");
      for (const p of recPending.values()) p.reject(err);
      recPending.clear();
      recPort = null;
    });
  }
  const id = ++recSeq;
  return new Promise((resolve, reject) => {
    recPending.set(id, { resolve, reject });
    recPort.postMessage({ ...args, id, cmd });     // the request id always wins
  });
}

function closeHost() {
  if (recPort && !recPending.size) { recPort.disconnect(); recPort = null; }
}

const active = () => rec && (rec.status === "starting" || rec.status === "recording");
const busy = () => recStarting || active() || rec?.status === "saving";

async function recorderMessage(r, msg) {
  return r.mode === "tab"
    ? chrome.runtime.sendMessage({ ...msg, target: "tab-recorder" })
    : chrome.tabs.sendMessage(r.tabId, msg, { frameId: r.frameId });
}

async function openTabRecorder() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (!contexts.length) await chrome.offscreen.createDocument({
    url: "record-tab.html", reasons: ["USER_MEDIA"],
    justification: "Record tab audio and keep it audible while the panel is closed",
  });
}

async function closeTabRecorder(r) {
  if (r?.mode === "tab") await chrome.offscreen.closeDocument().catch(() => {});
}

function publish(patch = {}) {
  if (rec) Object.assign(rec, patch);
  chrome.storage.session.set({ recording: rec });     // a restarted worker picks it up
  chrome.runtime.sendMessage({ type: "rec-update", recording: rec }).catch(() => {});
  badge();
}

// The toolbar badge: red while recording, with the minutes so far.
function badge() {
  clearInterval(badgeTimer);
  if (!active()) {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "Transcriber" });
    return;
  }
  const show = () => {
    const min = Math.floor((Date.now() - rec.startedAt) / 60000);
    chrome.action.setBadgeText({ text: min < 1 ? "REC" : `${min}m` });
    chrome.action.setTitle({ title: `Recording · ${rec.source} · ${min} min` });
  };
  chrome.action.setBadgeBackgroundColor({ color: "#B23A26" });
  chrome.action.setBadgeTextColor?.({ color: "#FFFFFF" });
  show();
  badgeTimer = setInterval(show, 15000);
}

async function recStart(msg) {
  if (busy()) throw new Error("A recording is starting, recording or saving — wait for it to finish");
  recStarting = true;
  try {
    await startRecorder(msg);
  } finally {
    recStarting = false;
  }
}

async function startRecorder(msg) {
  if (msg.mode === "tab") await openTabRecorder();
  let r;
  try {
    r = await hostCall("rec_start", {
      title: msg.title, page_url: msg.pageUrl, source: msg.source, live: !!msg.live,
    });
  } catch (error) {
    await closeTabRecorder(msg);
    closeHost();
    throw error;
  }
  rec = {
    mode: msg.mode || "element",
    recId: r.rec_id, tabId: msg.tabId, frameId: msg.frameId, title: msg.title,
    source: msg.source, pageUrl: msg.pageUrl, live: !!msg.live, startedAt: Date.now(),
    status: "starting", bytes: 0, playing: true, warning: null, note: null, error: null,
  };
  publish();
  let res;
  try {
    // Request the one-use ID only when the offscreen document and host are ready.
    const streamId = rec.mode === "tab"
      ? await chrome.tabCapture.getMediaStreamId({ targetTabId: msg.tabId }) : undefined;
    res = await recorderMessage(rec, {
      type: "rec-begin", recId: r.rec_id, key: msg.key, seg: 0, bitrate: r.bitrate,
      streamId,
      silenceStop: r.silence_stop, silenceStopLive: r.silence_stop_live,
    });
  } catch (e) {
    res = { ok: false, error: rec.mode === "tab"
      ? `${e.message} — click the Transcriber toolbar icon on the target tab, then try again`
      : "The page didn't answer — rescan and try again" };
  }
  if (!res?.ok) {
    await closeTabRecorder(rec);
    rec = null;
    publish();
    await hostCall("rec_discard", { rec_id: r.rec_id }).catch(() => {});   // nothing was recorded
    closeHost();
    throw new Error(res?.error || "Couldn't start recording");
  }
  if (rec?.recId === r.rec_id && rec.status === "starting") publish({ status: "recording" });
}

async function recChunk(msg) {
  if (!rec || msg.recId !== rec.recId) throw new Error("No such recording");
  const r = await hostCall("rec_chunk", { rec_id: msg.recId, seg: msg.seg, data: msg.data });
  publish({ bytes: r.bytes });
}

async function recStop(reason) {
  if (!active()) return;
  const current = rec;
  try {
    const res = await recorderMessage(current, { type: "rec-end", recId: current.recId, reason });
    if (res?.ok) return;     // the page flushes its last chunk, then sends rec-finished
  } catch { /* page or tab gone: close it from here */ }
  await recFinish(current.recId, reason);
}

// Discard: the page stops as for Stop, and once its last chunk is in, the
// folder is deleted instead of joined and transcribed (recFinish below).
async function recDiscard() {
  if (!active()) return;
  rec.discard = true;
  await recStop("discarded");
}

async function recFinish(recId, reason) {
  if (!rec || rec.recId !== recId || !active()) return;
  const current = rec;
  publish({ status: "saving", reason });
  await closeTabRecorder(current);
  if (rec.discard) {
    try { await hostCall("rec_discard", { rec_id: recId }); } catch { /* folder stays; harmless */ }
    rec = null;
    publish();
    closeHost();
    return;
  }
  try {
    const r = await hostCall("rec_stop", { rec_id: recId, reason, transcribe: false });
    publish({ status: "saved", duration: r.duration, bytes: r.bytes, path: r.path, jobId: r.job_id });
  } catch (e) {
    publish({ status: "failed", error: e.message });
  }
  closeHost();
}

const REC_HANDLERS = {
  "rec-start": (msg) => recStart(msg),
  "rec-stop": () => recStop("stopped"),
  "rec-discard": () => recDiscard(),
  "rec-chunk": (msg) => recChunk(msg),
  "rec-finished": (msg) => {
    if (msg.error && rec?.recId === msg.recId) publish({ error: msg.error });
    return recFinish(msg.recId, msg.reason);
  },
  "rec-state": async (msg) => {
    if (!rec || msg.recId !== rec.recId) return;
    const patch = {};
    for (const k of ["playing", "warning", "note", "error"]) if (k in msg) patch[k] = msg[k];
    publish(patch);
  },
  "rec-status": async () => ({ recording: rec }),
  "rec-dismiss": async () => { if (!busy()) { rec = null; publish(); } },
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target === "tab-recorder") return;
  const handler = REC_HANDLERS[msg?.type];
  if (!handler) return;
  recReady.then(() => handler(msg)).then(
    (out) => sendResponse({ ok: true, ...(out || {}) }),
    (e) => sendResponse({ ok: false, error: e.message }),
  );
  return true;      // answered asynchronously
});

// The tab went away or loaded another page: the page-side recorder is gone
// with it. Everything sent so far is on disk — close the recording on that.
chrome.tabs.onRemoved.addListener((tabId) => {
  if (active() && rec.tabId === tabId) recStop("the tab was closed");
});
// "loading" also fires for a single-page app changing its URL (soholms is
// one), where the recorder lives on — so ask it before closing anything.
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (!active() || rec.mode === "tab" || rec.tabId !== tabId || change.status !== "loading") return;
  const { recId, frameId } = rec;
  setTimeout(async () => {
    if (!active() || rec.recId !== recId) return;
    let alive = false;
    try {
      alive = (await chrome.tabs.sendMessage(tabId, { type: "rec-ping", recId }, { frameId }))?.ok;
    } catch { /* no recorder there any more */ }
    if (!alive) recFinish(recId, "the page reloaded");
  }, 2000);
});

// A service worker restarted mid-recording has lost its variables, not the
// recording: take it back from session storage.
const recReady = chrome.storage.session.get("recording").then(({ recording }) => {
  if (recording && !rec) { rec = recording; badge(); }
});
