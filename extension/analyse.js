// The Analyse side: the tab's list, the in-row prompt picker, the analysis
// screen (with follow-ups and the verdict), and the prompt library.
//
// Rules from CLAUDE.md that shape this file:
//   - a prompt is chosen on the row, when Analyse is pressed; nothing
//     remembers a "selected prompt" between runs
//   - an analysis is a background job; the panel never waits on one
//   - the verdict buttons stay visually neutral — they are the skip-rate metric

const ui = {
  picker: null,        // { videoId, mode: "list" | "ask" } — the one expanded row
  error: null,
  threadId: null,      // the analysis screen's thread, while it is open
  thread: null,
  threadSig: null,
  threadTimer: null,
  scrollToEnd: false,
  formOriginal: null,  // the prompt being edited; null for a new one
};

const THREAD_POLL = 2000;
const VERDICT_LABELS = [["watched", "Watched"], ["partial", "Partly"], ["skipped", "Skipped"]];

const ICON_A = {
  plus: '<svg viewBox="0 0 16 16"><path d="M8 3.5v9M3.5 8h9"/></svg>',
  x: '<svg viewBox="0 0 16 16"><path d="m4 4 8 8M12 4l-8 8"/></svg>',
  up: '<svg viewBox="0 0 16 16"><path d="m4 10 4-4 4 4"/></svg>',
  send: '<svg viewBox="0 0 16 16"><path d="M2.5 8h11M9 3.5 13.5 8 9 12.5"/></svg>',
};

// --- small helpers ---------------------------------------------------------

const promptLabel = (p) => (p === "custom" ? "ask" : p);

function mmss(sec) {
  sec = Math.max(0, Math.floor(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function fmtLen(sec) {
  if (sec == null) return null;
  sec = Math.round(sec);
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  return s ? `${m} min ${String(s).padStart(2, "0")} s` : `${m} min`;
}

function relTime(iso) {
  const then = new Date(iso), diff = (Date.now() - then) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  if (then >= today) return `${Math.floor(diff / 3600)} h ago`;
  if (then >= today - 86400000) return "yesterday";
  return then.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function iconButton(cls, label, svg, onClick) {
  const b = el("button", cls);
  b.type = "button";
  b.setAttribute("aria-label", label);
  b.title = label;
  b.innerHTML = svg;
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(e); });
  return b;
}

function videoMeta(videoId) {
  const r = rowFor(videoId) || state.threads.find((t) => t.video_id === videoId) || {};
  return { title: r.title || videoId, sub: [r.channel, fmtDuration(r.duration)].filter(Boolean).join(" · ") };
}

// Tell background.js there is something to announce when it finishes, so the
// notification fires even if the panel is closed by then.
function watchInBackground() {
  chrome.runtime.sendMessage({ type: "watch" }).catch(() => {});
}

// Keyed list update: a node is rebuilt only when its signature changes, so
// spinners keep spinning and a half-typed question survives every poll.
function reconcile(list, items) {
  const cache = list._cache || (list._cache = new Map());
  const seen = new Set();
  let prev = null;
  for (const it of items) {
    seen.add(it.key);
    let c = cache.get(it.key);
    if (!c || c.sig !== it.sig) {
      const node = it.build();
      if (c) c.node.replaceWith(node);
      c = { sig: it.sig, node };
      cache.set(it.key, c);
    }
    const want = prev ? prev.nextSibling : list.firstChild;
    if (want !== c.node) list.insertBefore(c.node, want);
    prev = c.node;
  }
  for (const [k, c] of cache) {
    if (!seen.has(k)) { c.node.remove(); cache.delete(k); }
  }
}

// --- actions ---------------------------------------------------------------

function startAnalyse(videoId) {
  if (!videoId) return;
  closeAllScreens();
  setTab("analyse");
  ui.picker = { videoId, mode: "list" };
  renderAnalyse();
  $("analyse-list").scrollTop = 0;
}

async function runAnalysis(videoId, args) {
  ui.picker = null;
  ui.error = null;
  try {
    await host.call("analyse", { video_id: videoId, ...args });
    watchInBackground();
  } catch (e) {
    ui.error = e.message;
  }
  renderAnalyse();
  refresh();
}

async function jobAction(cmd, jobId) {
  try { await host.call(cmd, { job_id: jobId }); } catch (e) { ui.error = e.message; }
  refresh();
  if (ui.threadId != null) loadThread();
}

async function seek(videoId, t) {
  const [tab] = await chrome.tabs.query({ active: true, windowId: windowId ?? chrome.windows.WINDOW_ID_CURRENT });
  if (tab && videoIdFrom(tab.url || "") === videoId) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (s) => {
          const v = document.querySelector("video");
          if (v) { v.currentTime = s; v.play(); }
        },
        args: [t],
      });
      return;
    } catch { /* fall through to a new tab */ }
  }
  chrome.tabs.create({ url: `https://www.youtube.com/watch?v=${videoId}&t=${t}s` });
}

// --- the tab ---------------------------------------------------------------

function sectionHeader(key, text) {
  return { key: `h-${key}`, sig: text, build: () => el("h2", "section", text) };
}

function renderAnalyse() {
  $("prompt-count").textContent = state.prompts.items.length || "";
  const list = $("analyse-list");
  const items = [];

  if (!state.connected) {
    items.push({ key: "offline", sig: "", build: () => el("p", "empty", "Host not connected — see the Transcribe tab.") });
    reconcile(list, items);
    return;
  }
  if (ui.error) {
    items.push({ key: "error", sig: ui.error, build: () => {
      const d = el("div", "banner bad");
      d.append(el("span", null, ui.error),
        iconButton("icon-btn small", "Dismiss", ICON_A.x, () => { ui.error = null; renderAnalyse(); }));
      return d;
    } });
  }

  for (const j of state.analysisJobs) {
    items.push({ key: `job-${j.job_id}`, sig: JSON.stringify(j), build: () => buildJobCard(j) });
  }

  const analysed = new Set(state.threads.map((t) => t.video_id));
  const pending = new Set(state.analysisJobs.filter((j) => j.status !== "failed").map((j) => j.video_id));
  const fresh = state.rows.filter((r) => r.state === "done" && r.has_transcript
    && !analysed.has(r.video_id) && !pending.has(r.video_id));

  const p = ui.picker;
  if (p && !fresh.some((r) => r.video_id === p.videoId)) {
    items.push(sectionHeader("again", "Analyse"));
    items.push(pickerItem(p));
  }
  if (state.threads.length) {
    items.push(sectionHeader("done", "Done"));
    for (const t of state.threads) {
      items.push({ key: `t-${t.id}`, sig: JSON.stringify(t) + relTime(t.updated_at), build: () => buildThreadRow(t) });
    }
  }
  if (fresh.length) {
    items.push(sectionHeader("fresh", "Transcribed, not analysed"));
    for (const r of fresh) {
      if (p && p.videoId === r.video_id) items.push(pickerItem(p));
      else items.push({ key: `f-${r.video_id}`, sig: JSON.stringify([r.title, r.channel, r.duration]), build: () => buildFreshRow(r) });
    }
  }
  if (!items.length) {
    items.push({ key: "empty", sig: "", build: () => el("p", "empty", "Transcribe a video first — then analyse it here.") });
  }
  reconcile(list, items);

  if (screenStack.includes("library")) renderLibrary();
}

function tickAnalyse() {
  const now = Date.now() / 1000;
  for (const e of document.querySelectorAll("[data-started]")) {
    e.textContent = mmss(now - Number(e.dataset.started));
  }
}

function elapsedSpan(started) {
  const s = el("span", null, mmss(Date.now() / 1000 - started));
  s.dataset.started = started;
  return s;
}

function buildJobCard(j) {
  const card = el("div", `card job ${j.status}`);
  const glyph = el("span", "glyph");
  const body = el("div", "body");
  const title = el("div", "title", j.title || j.video_id);
  const sub = el("div", "sub");
  const label = j.kind === "follow-up" ? "follow-up" : promptLabel(j.prompt);
  body.append(title, sub);
  card.append(glyph, body);

  if (j.status === "failed") {
    glyph.classList.add("bad");
    glyph.innerHTML = ICON.alert;
    sub.textContent = `${label} failed — ${j.error || "unknown error"}`;
    sub.title = sub.textContent;
    const actions = el("div", "actions");
    if (j.kind === "analyse") {
      const retry = el("button", "retry", "Retry");
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        await host.call("dismiss", { job_id: j.job_id }).catch(() => {});
        runAnalysis(j.video_id, j.prompt === "custom" ? { question: j.question } : { prompt: j.prompt });
      });
      actions.append(retry);
    }
    actions.append(iconButton("icon-btn small", "Dismiss", ICON_A.x, () => jobAction("dismiss", j.job_id)));
    card.append(actions);
    return card;
  }

  glyph.classList.add(j.status === "running" ? "spin" : "plain");
  if (j.status === "running") glyph.innerHTML = ICON.spin;
  if (j.status === "queued") {
    sub.textContent = `${label} · queued`;
  } else if (j.cancelling) {
    sub.textContent = `${label} · stopping…`;
  } else {
    sub.append(`${label} · running `, elapsedSpan(j.started || Date.now() / 1000), " · keep browsing");
  }
  if (j.question && j.kind === "analyse") title.title = j.question;
  card.append(iconButton("icon-btn small", j.status === "queued" ? "Remove from queue" : "Stop",
    ICON_A.x, () => jobAction("cancel", j.job_id)));
  return card;
}

function buildThreadRow(t) {
  const card = el("div", "card thread openable");
  card.tabIndex = 0;
  const dot = el("span", `unread-dot${t.unread ? " on" : ""}`);
  const body = el("div", "body");
  const meta = el("div", "meta-line");
  meta.append(el("span", "chip clay", promptLabel(t.prompt)), el("span", null, relTime(t.updated_at)));
  if (t.followups) meta.append(el("span", null, `· ${t.followups} follow-up${t.followups === 1 ? "" : "s"}`));
  if (t.unread) meta.append(el("span", null, "· unread"));
  body.append(el("div", "title", t.title || t.video_id), el("div", "sub", t.channel || ""), meta);
  if (t.verdict) {
    const label = VERDICT_LABELS.find(([v]) => v === t.verdict)?.[1] || t.verdict;
    body.append(el("div", "verdict-line", `✓ ${label.toLowerCase()}`));
  }
  const chev = el("span", "chev");
  chev.innerHTML = ICON.chev;
  card.append(dot, body, chev);
  card.addEventListener("click", () => openThread(t.id));
  card.addEventListener("keydown", (e) => { if (e.key === "Enter") openThread(t.id); });
  return card;
}

function buildFreshRow(r) {
  const card = el("div", "card fresh");
  const body = el("div", "body");
  body.append(el("div", "title", r.title || r.video_id),
    el("div", "sub", [r.channel, fmtDuration(r.duration)].filter(Boolean).join(" · ")));
  const btn = el("button", "outline", "Analyse");
  btn.addEventListener("click", () => { ui.picker = { videoId: r.video_id, mode: "list" }; renderAnalyse(); });
  card.append(body, btn);
  return card;
}

// --- the picker: Analyse expands the row into the choices -----------------

function pickerItem(p) {
  const promptsSig = p.mode === "list" ? JSON.stringify(state.prompts) : "";
  return { key: `pick-${p.videoId}-${p.mode}`, sig: promptsSig, build: () => buildPicker(p) };
}

function buildPicker(p) {
  const { title, sub } = videoMeta(p.videoId);
  const card = el("div", "card picker");
  const head = el("div", "picker-head");
  const text = el("div", "body");
  text.append(el("div", "title", title), el("div", "sub", sub));
  head.append(text, iconButton("icon-btn small", "Close", ICON_A.x, () => { ui.picker = null; renderAnalyse(); }));
  card.append(head);

  if (p.mode === "ask") {
    const form = el("form", "ask-form");
    const ta = el("textarea");
    ta.placeholder = "What do you want to know about this video?";
    ta.rows = 3;
    const foot = el("div", "ask-foot");
    const send = el("button", "primary clay compact");
    send.type = "submit";
    send.innerHTML = `Ask ${ICON_A.send}`;
    foot.append(el("span", "hint", "Enter to send · Shift+Enter for a new line"), send);
    form.append(ta, foot);
    const submit = () => {
      const q = ta.value.trim();
      if (q) runAnalysis(p.videoId, { question: q });
    };
    form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    card.append(form);
    setTimeout(() => ta.focus(), 0);
    return card;
  }

  const opts = el("ul", "options");
  const option = (cls, name, desc, onClick, tag) => {
    const li = el("li", `option ${cls}`);
    const b = el("button");
    b.type = "button";
    const g = el("span", "opt-glyph");
    if (cls === "ask") g.innerHTML = ICON_A.plus;
    const t = el("span", "opt-text");
    t.append(el("span", "opt-name", name));
    if (desc) t.append(el("span", "opt-desc", desc));
    b.append(g, t);
    if (tag) b.append(el("span", "tag-last", tag));
    b.addEventListener("click", onClick);
    li.append(b);
    return li;
  };
  opts.append(option("ask", "ask", "A question of your own, just this once", () => {
    ui.picker = { videoId: p.videoId, mode: "ask" };
    renderAnalyse();
  }));
  for (const pr of state.prompts.items) {
    opts.append(option("prompt", pr.name, pr.description,
      () => runAnalysis(p.videoId, { prompt: pr.name }),
      pr.name === state.prompts.last ? "LAST" : null));
  }
  if (!state.prompts.items.length) {
    const li = el("li", "option-note");
    const add = el("button", "text-btn", "add one");
    add.addEventListener("click", () => openPromptForm());
    li.append("No saved prompts yet — ", add);
    opts.append(li);
  }
  card.append(opts);
  return card;
}

// --- the analysis screen ---------------------------------------------------

async function openThread(id) {
  ui.threadId = id;
  ui.thread = null;
  ui.threadSig = null;
  const t = state.threads.find((x) => x.id === id);
  $("an-title").textContent = t?.title || "";
  $("an-prompt").textContent = t ? promptLabel(t.prompt) : "";
  $("an-meta").textContent = "";
  $("an-body").replaceChildren(el("p", "loading", "Loading…"));
  $("an-body").scrollTop = 0;
  $("an-question").value = "";
  $("an-error").hidden = true;
  disarm($("an-delete"));
  openScreen("analysis");
  await loadThread();
  refresh();      // opening marked it read: the unread dot and badge change
}

async function loadThread() {
  clearTimeout(ui.threadTimer);
  const id = ui.threadId;
  if (id == null) return;
  let d;
  try {
    d = await host.call("thread", { analysis_id: id });
  } catch (e) {
    if (ui.threadId === id) $("an-body").replaceChildren(el("p", "loading", e.message));
    return;
  }
  if (ui.threadId !== id) return;
  ui.thread = d;
  renderThread();
  if (d.pending.some((p) => p.status === "queued" || p.status === "running")) {
    ui.threadTimer = setTimeout(loadThread, THREAD_POLL);
  }
}

function onScreenClosed(id) {
  if (id === "analysis") {
    ui.threadId = null;
    clearTimeout(ui.threadTimer);
  }
}

function question(text) {
  const q = el("div", "question");
  q.append(el("p", null, text));
  return q;
}

function answer(md) {
  const a = el("div", "answer md");
  a.innerHTML = Markdown.render(md);   // escaped and tag-whitelisted in markdown.js
  return a;
}

function verdictCard(d) {
  const card = el("div", "verdict");
  card.append(el("p", null, "And the video?"));
  const row = el("div", "verdict-buttons");
  for (const [value, label] of VERDICT_LABELS) {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(d.verdict === value));
    b.addEventListener("click", async () => {
      const next = d.verdict === value ? null : value;
      try {
        await host.call("verdict", { analysis_id: d.analysis_id, verdict: next });
        d.verdict = next;
        ui.threadSig = null;
        renderThread();
        refresh();
      } catch (e) { showAskError(e.message); }
    });
    row.append(b);
  }
  card.append(row);
  return card;
}

function pendingBlock(d, p) {
  const frag = document.createDocumentFragment();
  frag.append(question(p.question));
  const s = el("div", `pending ${p.status}`);
  if (p.status === "failed") {
    s.append(el("span", null, `Failed — ${p.error || "unknown error"}`));
    const again = el("button", "text-btn", "Ask again");
    again.addEventListener("click", async () => {
      await host.call("dismiss", { job_id: p.job_id }).catch(() => {});
      sendFollowUp(p.question);
    });
    s.append(again, iconButton("icon-btn small", "Dismiss", ICON_A.x, () => jobAction("dismiss", p.job_id)));
  } else {
    const g = el("span", "glyph spin");
    g.innerHTML = ICON.spin;
    s.append(g);
    if (p.status === "queued") s.append(el("span", null, "queued"));
    else s.append(el("span", null, "working · "), elapsedSpan(p.started || Date.now() / 1000));
  }
  frag.append(s);
  return frag;
}

function renderThread() {
  const d = ui.thread;
  if (!d) return;
  const root = d.entries[0];
  const nFollow = d.entries.length - 1;
  $("an-title").textContent = d.title || d.video_id;
  $("an-prompt").textContent = promptLabel(d.prompt);
  $("an-meta").textContent = nFollow ? `+ ${nFollow} follow-up${nFollow === 1 ? "" : "s"}`
    : fmtLen(root?.duration_s) || "";

  const ask = $("an-question");
  ask.disabled = !d.resumable;
  $("an-send").disabled = !d.resumable;
  ask.placeholder = d.resumable ? "Ask about this analysis…"
    : "Made before follow-ups existed — can't be continued";

  const sig = JSON.stringify([d.entries.map((e) => e.id), d.pending, d.verdict]);
  if (sig === ui.threadSig) return;
  ui.threadSig = sig;

  const body = $("an-body");
  const top = body.scrollTop;
  const frag = document.createDocumentFragment();
  if (!root) {
    frag.append(el("p", "loading", "The analysis file is missing."));
    body.replaceChildren(frag);
    return;
  }
  const hasMore = nFollow > 0 || d.pending.length > 0;
  if (hasMore) {
    const pill = el("button", "back-pill");
    pill.type = "button";
    pill.innerHTML = `${ICON_A.up} Back to the ${promptLabel(d.prompt)}`;
    pill.addEventListener("click", () => body.scrollTo({ top: 0, behavior: "smooth" }));
    frag.append(pill);
  }
  if (root.question) frag.append(question(root.question));
  const first = answer(root.body);
  first.classList.add("first");
  frag.append(first, verdictCard(d));
  for (const e of d.entries.slice(1)) frag.append(question(e.question), answer(e.body));
  for (const p of d.pending) frag.append(pendingBlock(d, p));

  body.replaceChildren(frag);
  if (ui.scrollToEnd) {
    body.scrollTop = body.scrollHeight;
    ui.scrollToEnd = false;
  } else {
    body.scrollTop = top;
  }
  updateBackPill();
}

// The pill shows once the original answer has scrolled away.
function updateBackPill() {
  const body = $("an-body");
  const pill = body.querySelector(".back-pill");
  const first = body.querySelector(".answer.first");
  if (!pill || !first) return;
  pill.classList.toggle("shown", body.scrollTop > first.offsetTop + first.offsetHeight - 40);
}

function showAskError(msg) {
  const e = $("an-error");
  e.textContent = msg || "";
  e.hidden = !msg;
}

async function sendFollowUp(q) {
  if (!q || ui.threadId == null) return;
  showAskError("");
  try {
    await host.call("follow_up", { analysis_id: ui.threadId, question: q });
  } catch (e) {
    showAskError(e.message);
    return false;
  }
  watchInBackground();
  ui.scrollToEnd = true;
  loadThread();
  refresh();
  return true;
}

function threadMarkdown() {
  const d = ui.thread;
  if (!d) return "";
  const parts = [`${d.title} — ${promptLabel(d.prompt)}`, ""];
  d.entries.forEach((e, i) => {
    if (e.question) parts.push(`**Q:** ${e.question}`, "");
    parts.push(e.body, "");
    if (i === 0 && d.entries.length > 1) parts.push("---", "");
  });
  return parts.join("\n");
}

// --- prompt library and form -----------------------------------------------

function openLibrary() {
  renderLibrary();
  openScreen("library");
}

function renderLibrary() {
  const items = state.prompts.items;
  $("lib-count").textContent = `${items.length} saved`;
  const body = $("lib-body");
  const sig = JSON.stringify(items);
  if (body._sig === sig) return;
  body._sig = sig;

  const frag = document.createDocumentFragment();
  const add = el("button", "card add-card");
  const g = el("span", "opt-glyph filled");
  g.innerHTML = ICON_A.plus;
  const t = el("span", "opt-text");
  t.append(el("span", "opt-name", "add a prompt"), el("span", "opt-desc", "Name, one-line description, and the prompt itself"));
  add.append(g, t);
  add.addEventListener("click", () => openPromptForm());
  frag.append(add);

  for (const p of items) {
    const card = el("button", "card lib-item");
    const text = el("span", "body");
    text.append(el("span", "title", p.name));
    if (p.description) text.append(el("span", "sub wrap", p.description));
    // "never used" is the signal to delete or rewrite it.
    const stats = p.uses ? `used ${p.uses}×${p.avg_s ? ` · avg ${fmtLen(p.avg_s)}` : ""}` : "never used";
    text.append(el("span", "stats", stats));
    const chev = el("span", "chev");
    chev.innerHTML = ICON.chev;
    card.append(text, chev);
    card.addEventListener("click", () => openPromptForm(p.name));
    frag.append(card);
  }
  body.replaceChildren(frag);
}

function setNameHint() {
  const name = $("pf-name").value || "<name>";
  $("pf-name-hint").textContent = `Saved as prompts/${name}.md · this is what you pick from the list`;
}

function showFormError(msg) {
  const e = $("pf-error");
  e.textContent = msg || "";
  e.hidden = !msg;
}

async function openPromptForm(name = null) {
  ui.formOriginal = name;
  $("pf-heading").textContent = name ? "Edit prompt" : "New prompt";
  const del = $("pf-delete");
  del.hidden = !name;
  disarm(del);
  $("pf-name").value = name || "";
  $("pf-desc").value = "";
  $("pf-body").value = "";
  showFormError("");
  setNameHint();
  openScreen("prompt-form");
  if (name) {
    try {
      const p = await host.call("prompt_get", { name });
      $("pf-desc").value = p.description || "";
      $("pf-body").value = p.body || "";
    } catch (e) { showFormError(e.message); }
  } else {
    setTimeout(() => $("pf-name").focus(), 300);
  }
}

async function savePrompt() {
  const payload = {
    name: $("pf-name").value.trim(),
    description: $("pf-desc").value.trim(),
    body: $("pf-body").value,
    original: ui.formOriginal,
  };
  if (!payload.name) return showFormError("Give it a name");
  if (!payload.body.trim()) return showFormError("The prompt is empty");
  try {
    await host.call("prompt_save", payload);
  } catch (e) {
    return showFormError(e.message);
  }
  closeScreen("prompt-form");
  refresh();
}

async function deletePrompt() {
  try {
    await host.call("prompt_delete", { name: ui.formOriginal });
  } catch (e) {
    return showFormError(e.message);
  }
  closeScreen("prompt-form");
  refresh();
}

// --- wiring ----------------------------------------------------------------

$("lib-open").addEventListener("click", openLibrary);
$("prompt-add").addEventListener("click", () => openPromptForm());
$("lib-back").addEventListener("click", () => closeScreen("library"));

$("pf-back").addEventListener("click", () => closeScreen("prompt-form"));
$("pf-cancel").addEventListener("click", () => closeScreen("prompt-form"));
$("pf-save").addEventListener("click", savePrompt);
$("pf").addEventListener("submit", (e) => { e.preventDefault(); savePrompt(); });
confirmTwice($("pf-delete"), "Delete — sure?", deletePrompt);
$("pf-name").addEventListener("input", (e) => {
  const clean = e.target.value.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");
  if (clean !== e.target.value) e.target.value = clean;
  setNameHint();
});

$("an-back").addEventListener("click", () => closeScreen("analysis"));
$("an-transcript").addEventListener("click", () => ui.thread && openTranscript(ui.thread.video_id));
$("an-copy").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  await navigator.clipboard.writeText(threadMarkdown());
  flash(btn, "Copied");
});

// Soft delete of the whole thread. Follow-ups go with it (they answer a
// question nobody could see any more), so the first press asks the host how
// many there are and says so before the second press does anything.
confirmTwice($("an-delete"), async () => {
  if (!ui.thread) return "Delete — sure?";
  try {
    const r = await host.call("delete_analysis", { analysis_id: ui.thread.analysis_id, dry_run: true });
    return r.followups ? `Delete + ${r.followups} follow-up${r.followups === 1 ? "" : "s"}?` : "Delete — sure?";
  } catch { return "Delete — sure?"; }
}, async () => {
  if (!ui.thread) return;
  try {
    await host.call("delete_analysis", { analysis_id: ui.thread.analysis_id });
  } catch (e) {
    return showAskError(e.message);
  }
  closeScreen("analysis");
  refresh();
});
$("an-body").addEventListener("click", (e) => {
  const ts = e.target.closest("a.ts");
  if (ts && ui.thread) {
    e.preventDefault();
    seek(ui.thread.video_id, Number(ts.dataset.t));
  }
});
$("an-body").addEventListener("scroll", updateBackPill, { passive: true });

const askBox = $("an-question");
function autosize() {
  askBox.style.height = "auto";
  askBox.style.height = `${Math.min(askBox.scrollHeight, 120)}px`;
}
askBox.addEventListener("input", () => { autosize(); showAskError(""); });
askBox.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("an-ask").requestSubmit();
  }
});
$("an-ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = askBox.value.trim();
  if (!q) return;
  askBox.disabled = true;
  const ok = await sendFollowUp(q);
  askBox.disabled = !(ui.thread?.resumable ?? true);
  if (ok) { askBox.value = ""; autosize(); }
});

// A notification click (background.js) leaves the thread to open here.
async function openPendingThread() {
  try {
    const { openThread: id } = await chrome.storage.session.get("openThread");
    if (id == null) return;
    await chrome.storage.session.remove("openThread");
    setTab("analyse");
    openThread(id);
  } catch { /* storage unavailable: nothing to open */ }
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "session" && changes.openThread?.newValue != null) openPendingThread();
});
openPendingThread();
