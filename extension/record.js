// Record screen, step 1: what on this tab can be recorded.
//
// "Choose what to record" injects record-probe.js into every frame of the
// active tab (allFrames: cross-origin iframes included, which is what
// <all_urls> in the manifest is for — players usually live in one). Each
// frame then reports its sources here on its own, as they appear, play,
// pause or vanish; there is no single answer to wait for, because an iframe
// may load its player seconds after the page.
//
// The list belongs to the tab it was scanned on. Switching tabs doesn't
// rescan — scanning happens when asked.

const rec = {
  tabId: null,
  title: "",
  url: "",
  frames: new Map(),    // frameId -> { url, top, title, sources }
  selected: null,       // "frameId:key", or "tab" for the whole tab
  scanning: false,
  error: null,
};

const REC_ICON = {
  video: '<svg viewBox="0 0 16 16"><rect x="1.5" y="3.5" width="9.5" height="9" rx="1.5"/><path d="m11 7 3.5-2v6L11 9"/></svg>',
  audio: '<svg viewBox="0 0 16 16"><path d="M2.5 6h2.5l3.5-3v10L5 10H2.5z"/><path d="M11 5.5a3.5 3.5 0 0 1 0 5M12.8 3.5a6 6 0 0 1 0 9"/></svg>',
};

async function openRecord() {
  const [tab] = await chrome.tabs.query({ active: true, windowId: windowId ?? chrome.windows.WINDOW_ID_CURRENT });
  if (!tab) return;
  if (rec.tabId !== tab.id) rec.selected = null;
  Object.assign(rec, { tabId: tab.id, title: tab.title || "", url: tab.url || "" });
  rec.frames.clear();
  openScreen("record");
  scanTab();
}

async function scanTab() {
  rec.scanning = true;
  rec.error = null;
  renderRecord();
  try {
    await chrome.scripting.executeScript({
      target: { tabId: rec.tabId, allFrames: true },
      files: ["record-probe.js"],
    });
  } catch (e) {
    // Judged by the tab's address, not the error's wording: "Cannot access
    // contents of the page" also means plain missing site access, and
    // hiding that behind "Chrome doesn't allow it" sent the owner nowhere.
    if (/^(chrome|edge|about|chrome-extension|devtools|view-source):/.test(rec.url)
        || /^https:\/\/chrome(webstore)?\.google\.com\/(webstore|detail)/.test(rec.url)) {
      rec.error = "Chrome doesn't let extensions into this page.";
    } else if (/cannot access|permission/i.test(e.message)) {
      rec.error = `${e.message} — check chrome://extensions → Transcriber → Details → `
        + "Site access: it must be “On all sites”.";
    } else {
      rec.error = e.message;
    }
  }
  rec.scanning = false;
  renderRecord();
}

function stopProbe() {
  if (rec.tabId != null) chrome.tabs.sendMessage(rec.tabId, { type: "probe-stop" }).catch(() => {});
}

function hostOf(url) {
  try { return new URL(url).hostname; } catch { return url; }
}

function sourceLabel(s) {
  const what = s.tag === "audio" ? "Audio" : "Video";
  const size = s.width ? ` ${s.width}×${s.height}` : "";
  return `${what}${size}${s.hidden ? " (hidden)" : ""}`;
}

function sourceSub(f, s) {
  const parts = [];
  if (s.duration === "live") parts.push("LIVE ●");
  else if (s.duration != null) parts.push(fmtDuration(s.duration));
  parts.push(s.ended ? "ended" : s.paused ? "paused" : "playing");
  if (s.muted) parts.push("muted");
  if (!f.top) parts.push(`in ${hostOf(f.url)}`);
  return parts.join(" · ");
}

function allSources() {
  const frames = [...rec.frames.entries()]
    .sort(([a, fa], [b, fb]) => (fb.top - fa.top) || a - b);
  return frames.flatMap(([frameId, f]) => f.sources.map((s) => ({ frameId, f, s })));
}

function buildSourceRow({ frameId, f, s }) {
  const id = `${frameId}:${s.key}`;
  const li = el("li", `row source${rec.selected === id ? " picked" : ""}${s.duration === "live" ? " live" : ""}`);
  const icon = el("span", "src-icon");
  icon.innerHTML = s.tag === "audio" ? REC_ICON.audio : REC_ICON.video;
  const body = el("div", "body");
  const title = el("div", "title", sourceLabel(s));
  const sub = el("div", "sub", sourceSub(f, s));
  sub.title = s.src;
  body.append(title, sub);
  if (s.drm) body.append(el("div", "flag", "DRM — only “Whole tab” can record it"));
  const show = el("button", "text-btn", "Show");
  show.title = "Scroll to it on the page";
  show.addEventListener("click", (e) => { e.stopPropagation(); revealSource(frameId, f, s.key); });
  li.append(icon, body, show);
  li.tabIndex = 0;
  const pick = () => { rec.selected = id; renderRecord(); };
  li.addEventListener("click", pick);
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  return li;
}

function buildWholeTabRow() {
  const li = el("li", `row source${rec.selected === "tab" ? " picked" : ""}`);
  const icon = el("span", "src-icon");
  icon.innerHTML = REC_ICON.audio;
  const body = el("div", "body");
  body.append(el("div", "title", "Whole tab"), el("div", "sub", "Everything this tab plays, whatever the player"));
  li.append(icon, body, el("span"));
  li.tabIndex = 0;
  const pick = () => { rec.selected = "tab"; renderRecord(); };
  li.addEventListener("click", pick);
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  return li;
}

async function revealSource(frameId, f, key) {
  // Inside an iframe, scrolling the frame isn't enough: the page must also
  // bring that iframe into view, so the top frame is asked too.
  if (!f.top) chrome.tabs.sendMessage(rec.tabId, { type: "probe-reveal-frame", url: f.url }, { frameId: 0 }).catch(() => {});
  chrome.tabs.sendMessage(rec.tabId, { type: "probe-flash", key }, { frameId }).catch(() => {});
}

function renderRecord() {
  if (!screenStack.includes("record")) return;
  $("rec-title").textContent = rec.title || "This tab";
  const sources = allSources();
  const frameCount = new Set(sources.map((x) => x.frameId)).size;
  $("rec-meta").textContent = rec.scanning ? "Scanning…"
    : sources.length ? `${sources.length} source${sources.length === 1 ? "" : "s"}`
      + (frameCount > 1 ? ` in ${frameCount} frames` : "") + ` · ${hostOf(rec.url)}`
    : hostOf(rec.url);

  const body = $("rec-body");
  const out = [];
  if (rec.error) out.push(el("p", "form-error", rec.error));
  const list = el("ol", "rows");
  for (const x of sources) list.append(buildSourceRow(x));
  // Tab audio capture does not depend on injecting a probe into the site.
  if (!rec.error || /^https?:/.test(rec.url)) list.append(buildWholeTabRow());
  out.push(list);
  if (!rec.error && !rec.scanning && !sources.length) {
    out.push(el("p", "hint rec-note",
      "No video or audio found yet. A player often appears only after you press play — "
      + "the list updates on its own when it does."));
  }
  body.replaceChildren(...out);
  renderRecNow();
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type !== "probe" || sender.tab?.id !== rec.tabId) return;
  if (msg.gone || !msg.sources.length) rec.frames.delete(sender.frameId);
  else rec.frames.set(sender.frameId, { url: msg.url, top: msg.top, title: msg.title, sources: msg.sources });
  if (rec.selected && rec.selected !== "tab"
      && !allSources().some(({ frameId, s }) => `${frameId}:${s.key}` === rec.selected)) {
    rec.selected = null;    // the picked source is gone
  }
  renderRecord();
});

// The scanned tab moved on while the screen is open: scan again, since the
// question "what can I record here" is still being asked. Not cleared on the
// spot — in a single-page app (soholms is one) the URL changes without a new
// document, the probes live on, and frames that really went away have
// already said so (pagehide). The delay lets a full load finish first.
let rescanTimer = null;
chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (tabId !== rec.tabId || !screenStack.includes("record")) return;
  if (!change.url && change.status !== "complete") return;
  if (change.url) rec.url = change.url;
  if (tab.title) rec.title = tab.title;
  clearTimeout(rescanTimer);
  rescanTimer = setTimeout(scanTab, 1200);
});

// --- recording (mode A) ----------------------------------------------------
//
// background.js owns the recording; this only shows it and sends start and
// stop. It stays correct when the panel is reopened mid-recording, because
// the state is asked for, not remembered here.

let recNow = null;      // background.js's view of the current or last recording

function selectedSource() {
  if (!rec.selected || rec.selected === "tab") return null;
  const all = allSources();
  const i = all.findIndex(({ frameId, s }) => `${frameId}:${s.key}` === rec.selected);
  return i === -1 ? null : { ...all[i], index: i, count: all.length };
}

function fmtBytes(n) {
  if (!n) return "0 KB";
  return n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const recActive = () => recNow && (recNow.status === "starting" || recNow.status === "recording");
let recCardSig = null;   // what the card was last built from, minus the size

function renderRecNow() {
  // The intake button says it is recording even with this screen closed.
  const open = $("record-open");
  open.classList.toggle("recording", !!recActive());
  open.lastChild.textContent = recActive()
    ? ` Recording · ${fmtDuration((Date.now() - recNow.startedAt) / 1000)}`
    : " Choose what to record";

  const box = $("rec-now");
  box.hidden = !recNow;
  const go = $("rec-go");
  const picked = selectedSource();
  go.disabled = !!recActive() || recNow?.status === "saving" || recStarting || (!picked && rec.selected !== "tab");
  $("rec-go-label").textContent = rec.selected === "tab" ? "Record whole tab"
    : picked ? `Record ${sourceLabel(picked.s).toLowerCase()}` : "Pick what to record";
  if (!recNow) { recCardSig = null; return; }

  const r = recNow;
  const elapsed = fmtDuration((Date.now() - r.startedAt) / 1000);
  const what = {
    starting: "Starting…",
    recording: r.playing === false ? `Paused with the video · ${elapsed}` : `Recording · ${elapsed}`,
    saving: "Saving…",
    saved: `Saved · ${fmtDuration(r.duration) || "?"}`,
    failed: "Recording failed",
  }[r.status];
  // The clock and the size change every second; the card itself only when
  // something else does. Rebuilding it each second would also rebuild the
  // Discard button, and a two-press button that resets every second can
  // never be pressed twice.
  const { bytes, ...rest } = r;
  const sig = JSON.stringify(rest);
  if (sig === recCardSig && box.firstChild) {
    box.querySelector(".rec-what").textContent = what;
    box.querySelector(".rec-size").textContent = fmtBytes(bytes);
    return;
  }
  recCardSig = sig;
  const head = el("div", "rec-head");
  const dot = el("span", `rec-dot ${r.status}`);
  head.append(dot, el("strong", "rec-what", what), el("span", "muted rec-size", fmtBytes(bytes)));
  const lines = [head, el("div", "sub", [r.title, r.source].filter(Boolean).join(" · "))];
  if (r.warning) lines.push(el("div", "flag", r.warning));
  if (r.note) lines.push(el("div", "hint", r.note));
  if (r.error) lines.push(el("div", "form-error", r.error));
  if (r.status === "saved") {
    lines.push(el("div", "hint", `${r.reason ? `Stopped: ${r.reason}. ` : ""}`
      + "Audio saved. Use Transcribe in the list whenever you are ready."));
  }
  const actions = el("div", "rec-actions");
  if (recActive()) {
    const stopBtn = el("button", "tool", "Stop & save");
    stopBtn.addEventListener("click", async () => {
      stopBtn.disabled = true;
      await chrome.runtime.sendMessage({ type: "rec-stop" }).catch(() => {});
    });
    // Destructive, so two presses (CLAUDE.md): nothing is kept or transcribed.
    const discard = el("button", "tool danger");
    discard.append(el("span", null, "Discard"));
    confirmTwice(discard, "Discard it?", () => chrome.runtime.sendMessage({ type: "rec-discard" }).catch(() => {}));
    actions.append(stopBtn, discard);
  } else if (r.status === "saved" || r.status === "failed") {
    const done = el("button", "tool", "Done");
    done.addEventListener("click", () => chrome.runtime.sendMessage({ type: "rec-dismiss" }).catch(() => {}));
    actions.append(done);
  }
  lines.push(actions);
  box.replaceChildren(...lines);
}

let recStarting = false;
async function startRecording() {
  const p = selectedSource();
  const wholeTab = rec.selected === "tab";
  if (recStarting || (!p && !wholeTab)) return;
  recStarting = true;
  const err = $("rec-go-error");
  err.hidden = true;
  $("rec-go").disabled = true;
  // Several videos on one page (a lesson of five) would all get the page's
  // title: number them, in page order.
  const title = (rec.title || hostOf(rec.url)) + (!wholeTab && p.count > 1 ? ` — video ${p.index + 1}` : "");
  const res = await chrome.runtime.sendMessage({
    type: "rec-start", mode: wholeTab ? "tab" : "element", tabId: rec.tabId,
    frameId: p?.frameId, key: p?.s.key, title,
    pageUrl: rec.url, source: wholeTab ? "Whole tab" : `${sourceLabel(p.s)}${p.f.top ? "" : ` in ${hostOf(p.f.url)}`}`,
    live: wholeTab || p.s.duration === "live",
  }).catch((e) => ({ ok: false, error: e.message }));
  recStarting = false;
  if (!res?.ok) {
    err.textContent = res?.error || "Couldn't start recording";
    err.hidden = false;
  }
  renderRecNow();
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type !== "rec-update") return;
  const wasSaving = recNow?.status === "saving";
  recNow = msg.recording;
  if (wasSaving && recNow?.status === "saved") refresh();     // show the saved audio in the list
  renderRecNow();
});

setInterval(() => { if (recActive()) renderRecNow(); }, 1000);
chrome.runtime.sendMessage({ type: "rec-status" })
  .then((r) => { recNow = r?.recording || null; renderRecNow(); })
  .catch(() => {});

$("record-open").addEventListener("click", openRecord);
$("rec-back").addEventListener("click", () => closeScreen("record"));
$("rec-rescan").addEventListener("click", scanTab);
$("rec-go").addEventListener("click", startRecording);
