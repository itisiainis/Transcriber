// Channel screen: a channel's videos (channel.py via the host), sortable,
// tickable, and queued to transcribe one by one in the order shown. Opened
// from a channel page's intake button, or by pasting a channel link.
//
// Listing takes a few seconds, so the host does it on a thread and this asks
// again until it is done. Whether a video is already transcribed, queued or
// running comes from the main list (state.rows), so the screen stays live
// while its batch runs.

const LIST_POLL = 1200;     // ms between "is the listing done?" asks

const ch = {
  url: null, name: null,
  data: null,               // the host's listing: { channel, platform, videos, truncated }
  error: null,
  sort: "date", flip: false,
  picked: new Set(),
  timer: null, seq: 0,
  sig: null,                // what the list was last built from
};

// Each sort's natural direction first; pressing it again flips it.
const SORTS = {
  date:   { labels: ["Newest", "Oldest"],     cmp: (a, b) => a.index - b.index },
  title:  { labels: ["A–Z", "Z–A"],
            cmp: (a, b) => (a.title || "").localeCompare(b.title || "", undefined, { numeric: true, sensitivity: "base" }) },
  length: { labels: ["Shortest", "Longest"],  cmp: (a, b) => (a.duration ?? Infinity) - (b.duration ?? Infinity) },
};

function openChannel(url, name) {
  if (ch.url !== url) { ch.picked.clear(); ch.data = null; }
  ch.url = url;
  ch.name = name || null;
  ch.error = null;
  ch.sig = null;
  $("ch-error").hidden = true;
  openScreen("channel");
  $("ch-body").scrollTop = 0;
  loadChannel(false);
}

async function loadChannel(refresh) {
  clearTimeout(ch.timer);
  const seq = ++ch.seq;
  ch.error = null;
  if (refresh) { ch.data = null; ch.sig = null; }
  renderChannel();
  try {
    const r = await host.call("channel", { url: ch.url, refresh });
    if (seq !== ch.seq || !screenStack.includes("channel")) return;
    if (r.status === "loading") {
      ch.timer = setTimeout(() => loadChannel(false), LIST_POLL);
      return;
    }
    ch.data = r;
  } catch (e) {
    if (seq !== ch.seq) return;
    ch.error = e.message;
  }
  ch.sig = null;
  renderChannel();
}

// null = not transcribed yet; otherwise the main list's state for it.
function videoState(v) {
  const r = rowFor(v.video_id);
  if (r) return r.state;
  return v.present ? "done" : null;
}
const pickable = (v) => { const s = videoState(v); return s == null || s === "failed"; };

function sortedVideos() {
  const vs = [...(ch.data?.videos || [])].sort(SORTS[ch.sort].cmp);
  return ch.flip ? vs.reverse() : vs;
}

function picks() {
  return sortedVideos().filter((v) => ch.picked.has(v.video_id) && pickable(v));
}

function fmtListedDate(v) {
  if (!v.listed_date) return null;
  const d = new Date(`${v.listed_date}T12:00:00`);
  const text = d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  // YouTube's listing only says "3 weeks ago"; the date is worked back from that.
  return v.platform === "youtube" ? `≈ ${text}` : text;
}

const STATE_NOTE = { done: "transcribed", queued: "queued", running: "transcribing", failed: "failed — tick to retry" };

function buildPickRow(v) {
  const st = videoState(v);
  const can = st == null || st === "failed";
  const li = el("li", `row pick${st ? ` ${st}` : ""}`);
  let lead;
  if (can) {
    lead = el("input", "pick-box");
    lead.type = "checkbox";
    lead.tabIndex = -1;
    lead.checked = ch.picked.has(v.video_id);
    li.classList.toggle("picked", lead.checked);
  } else {
    lead = el("span", "glyph");
    if (st === "running") { lead.classList.add("spin"); lead.innerHTML = ICON.spin; }
    else if (st === "queued") lead.classList.add("plain");
    else { lead.classList.add(rowFor(v.video_id)?.analysed ? "analysed" : "done-mark"); lead.innerHTML = ICON.check; }
  }
  const body = el("div", "body");
  const title = el("div", "title", v.title || v.video_id);
  title.title = v.title || v.video_id;
  const sub = el("div", "sub",
    [fmtListedDate(v), fmtDuration(v.duration), can ? fmtEstimate(v.estimate) : STATE_NOTE[st]]
      .filter(Boolean).join(" · "));
  body.append(title, sub);
  li.append(lead, body, el("span"));

  const toggle = () => {
    if (ch.picked.has(v.video_id)) ch.picked.delete(v.video_id);
    else ch.picked.add(v.video_id);
    renderChannel();
  };
  if (can) {
    li.tabIndex = 0;
    li.addEventListener("click", toggle);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  } else if (st === "done" && rowFor(v.video_id)?.has_transcript) {
    li.classList.add("openable");
    li.tabIndex = 0;
    li.addEventListener("click", () => openTranscript(v.video_id));
  }
  return li;
}

function renderChannel() {
  if (!screenStack.includes("channel")) return;
  const d = ch.data;
  $("ch-title").textContent = d?.channel || ch.name || "Channel";

  for (const b of $("ch-sort").querySelectorAll("button")) {
    const on = b.dataset.sort === ch.sort;
    b.setAttribute("aria-pressed", String(on));
    b.textContent = SORTS[b.dataset.sort].labels[on && ch.flip ? 1 : 0];
  }

  const body = $("ch-body");
  if (!d) {
    $("ch-meta").textContent = "";
    $("ch-go").disabled = true;
    $("ch-go-label").textContent = "Select videos to transcribe";
    const msg = ch.error || "Listing the channel's videos…";
    if (ch.sig !== msg) { body.replaceChildren(el("p", "loading", msg)); ch.sig = msg; }
    return;
  }

  const vs = sortedVideos();
  const done = vs.filter((v) => videoState(v) === "done").length;
  $("ch-meta").textContent = [
    `${vs.length} video${vs.length === 1 ? "" : "s"}${d.truncated ? " (newest only)" : ""}`,
    done ? `${done} transcribed` : null,
  ].filter(Boolean).join(" · ");

  const fresh = vs.filter(pickable);
  const allPicked = fresh.length > 0 && fresh.every((v) => ch.picked.has(v.video_id));
  $("ch-pick-all").querySelector("span").textContent = allPicked ? "Clear selection" : "Select all new";
  $("ch-pick-all").disabled = !fresh.length;

  // The batch button carries the count and the total, recalculated per tick.
  const p = picks();
  const total = p.reduce((s, v) => s + (v.estimate || 0), 0);
  const unknown = p.some((v) => v.estimate == null);
  $("ch-go").disabled = !p.length;
  $("ch-go-label").textContent = p.length
    ? `Transcribe ${p.length} video${p.length === 1 ? "" : "s"} · ${fmtEstimate(total)}${unknown ? "+" : ""}`
    : "Select videos to transcribe";

  // Rebuilt only when something shown changed, so polling doesn't reset focus.
  const sig = JSON.stringify([ch.sort, ch.flip, [...ch.picked],
    vs.map((v) => [v.video_id, videoState(v), v.estimate, rowFor(v.video_id)?.analysed])]);
  if (sig === ch.sig) return;
  ch.sig = sig;
  if (!vs.length) {
    body.replaceChildren(el("p", "loading", "No videos on this channel."));
    return;
  }
  const list = el("ol", "rows");
  for (const v of vs) list.append(buildPickRow(v));
  const scroll = body.scrollTop;
  body.replaceChildren(list);
  body.scrollTop = scroll;
}

async function transcribePicked() {
  const p = picks();
  if (!p.length) return;
  const btn = $("ch-go");
  btn.disabled = true;
  $("ch-error").hidden = true;
  try {
    // In the order shown: that is the order they will run.
    await host.call("transcribe_many", {
      channel: ch.data.channel || ch.name,
      videos: p.map((v) => ({ video_id: v.video_id, title: v.title, duration: v.duration })),
    });
    ch.picked.clear();
  } catch (e) {
    $("ch-error").textContent = e.message;
    $("ch-error").hidden = false;
  }
  await refresh();
}

$("ch-back").addEventListener("click", () => closeScreen("channel"));
$("ch-refresh").addEventListener("click", () => loadChannel(true));
$("ch-go").addEventListener("click", transcribePicked);
$("ch-pick-all").addEventListener("click", () => {
  const fresh = sortedVideos().filter(pickable);
  const all = fresh.every((v) => ch.picked.has(v.video_id));
  for (const v of fresh) all ? ch.picked.delete(v.video_id) : ch.picked.add(v.video_id);
  renderChannel();
});
for (const b of $("ch-sort").querySelectorAll("button")) {
  b.addEventListener("click", () => {
    if (ch.sort === b.dataset.sort) ch.flip = !ch.flip;
    else { ch.sort = b.dataset.sort; ch.flip = false; }
    renderChannel();
  });
}
