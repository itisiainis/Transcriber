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
