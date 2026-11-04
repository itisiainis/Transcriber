// Injected on request — the panel's "Choose what to record" — into every
// frame of the tab (record.js: scripting.executeScript, allFrames). Never
// declared as a content script: nothing of the extension sits in pages the
// owner didn't ask about.
//
// In this frame it finds every <video>/<audio>, including inside open
// shadow roots (players build their UI in them), keeps the list current and
// reports it to the panel. "Current" takes three mechanisms, because none
// sees everything:
//   - a MutationObserver per root: elements added or removed. It does not
//     see into shadow roots, so each open one found gets its own.
//   - listeners on each element: play, pause, a new duration… Media events
//     don't bubble and don't cross a shadow boundary, so a capture listener
//     on document would miss players that keep their <video> in a shadow root.
//   - a slow poll, for what neither catches: a closed shadow root that opens
//     later, src swapped by a script without any event we listen to.
//
// Injecting twice (Rescan) must not stack observers: the second run only
// restarts the first.

(() => {
  if (window.__trProbe) {
    window.__trProbe.start();
    return;
  }

  const EVENTS = ["play", "playing", "pause", "ended", "emptied", "loadedmetadata",
                  "durationchange", "volumechange", "resize"];
  const POLL_MS = 3000;
  const DEBOUNCE_MS = 400;   // a busy SPA mutates constantly; one scan per burst

  const keys = new WeakMap();     // element -> key, stable for this frame's lifetime
  const watched = new WeakSet();  // elements carrying our listeners
  let observedRoots = new WeakSet();
  const observers = [];
  let seq = 0;
  let running = false;
  let timer = null;
  let poll = null;
  let last = "";                  // the last report sent, to skip identical ones

  function schedule() {
    if (!running || timer) return;
    timer = setTimeout(report, DEBOUNCE_MS);
  }

  function observe(root) {
    if (observedRoots.has(root)) return;
    observedRoots.add(root);
    const mo = new MutationObserver(schedule);
    mo.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
    observers.push(mo);
  }

  function collect(root, out = []) {
    for (const m of root.querySelectorAll("video, audio")) out.push(m);
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) {
        observe(el.shadowRoot);
        collect(el.shadowRoot, out);
      }
    }
    return out;
  }

  function describe(m) {
    if (!keys.has(m)) keys.set(m, `m${++seq}`);
    if (!watched.has(m)) {
      watched.add(m);
      for (const e of EVENTS) m.addEventListener(e, schedule);
    }
    const src = m.currentSrc || m.src || "";
    const d = m.duration;
    const r = m.getBoundingClientRect();
    return {
      key: keys.get(m),
      tag: m.tagName.toLowerCase(),
      // blob: = MediaSource (HLS/DASH via a JS player); http = a plain file.
      kind: src.startsWith("blob:") ? "stream" : /^https?:/.test(src) ? "file" : src ? "other" : "none",
      src: src.slice(0, 200),
      width: m.videoWidth || 0,
      height: m.videoHeight || 0,
      // JSON has no Infinity: a live stream reports "live".
      duration: d === Infinity ? "live" : Number.isFinite(d) ? d : null,
      paused: m.paused,
      ended: m.ended,
      muted: m.muted || m.volume === 0,
      drm: !!m.mediaKeys,
      // Preview loops, ad slots and preloaders are often 0×0 or 1×1.
      hidden: m.tagName === "VIDEO" && (r.width < 4 || r.height < 4),
    };
  }

  function send(msg) {
    try {
      chrome.runtime.sendMessage(msg).catch(() => {});   // nobody listening: the panel closed
    } catch {
      stop();     // the extension was reloaded under us; this copy is orphaned
    }
  }

  function report() {
    timer = null;
    if (!running) return;
    const sources = collect(document).map(describe);
    const body = JSON.stringify(sources);
    if (body === last) return;
    last = body;
    send({ type: "probe", url: location.href, top: window === window.top, title: document.title, sources });
  }

  function find(key) {
    return collect(document).find((m) => keys.get(m) === key);
  }

  // A short outline flash, so "which of the five videos is this row?" has an answer.
  function flash(el) {
    const before = { outline: el.style.outline, offset: el.style.outlineOffset };
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.style.outline = "4px solid #1E6C88";
    el.style.outlineOffset = "-4px";
    setTimeout(() => {
      el.style.outline = before.outline;
      el.style.outlineOffset = before.offset;
    }, 1600);
  }

  function start() {
    if (!running) {
      running = true;
      observe(document);
      poll = setInterval(schedule, POLL_MS);
    }
    last = "";          // a Rescan always answers, even if nothing changed
    report();
  }

  function stop() {
    running = false;
    clearTimeout(timer);
    clearInterval(poll);
    timer = poll = null;
    for (const mo of observers.splice(0)) mo.disconnect();
    observedRoots = new WeakSet();
  }

  // --- recording one element (mode A) ---------------------------------------
  //
  // captureStream() gives the element's own output as a MediaStream: only its
  // sound, the tab is not muted, and nothing else the browser plays gets in.
  // Only its audio tracks go to MediaRecorder (opus in WebM). A chunk every
  // 10 s goes straight to background.js and on to the host, which appends it
  // to a file — nothing is held here longer than one chunk.
  //
  // Runs independently of the scan above: closing the Record screen stops
  // the scan, never a recording.

  const CHUNK_MS = 10000;
  const SILENT_RMS = 0.002;     // below this a second counts as silence
  const UNHEARD_WARN_S = 3;     // playing this long and never a sound: say so

  let recording = null;

  function b64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  }

  // Sent one at a time, in order: the host appends, so order is the file.
  // A failed send is retried; a host that stays away loses that chunk, and
  // the panel shows the error.
  function queueChunk(r, seg, blob) {
    r.queue = r.queue.then(async () => {
      if (!blob.size) return;
      const data = b64(await blob.arrayBuffer());
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const res = await chrome.runtime.sendMessage({ type: "rec-chunk", recId: r.recId, seg, data });
          if (res?.ok) return;
          if (res?.error) throw new Error(res.error);
        } catch (e) {
          if (attempt === 4) recState({ error: `A chunk was lost: ${e.message}` });
        }
        await new Promise((ok) => setTimeout(ok, 1500 * (attempt + 1)));
      }
    });
  }

  function recState(patch) {
    try { chrome.runtime.sendMessage({ type: "rec-state", recId: recording?.recId, ...patch }).catch(() => {}); }
    catch { /* extension reloaded */ }
  }

  function onPause() { if (recording?.mr?.state === "recording") recording.mr.pause(); recState({ playing: false }); }
  function onPlay() { if (recording?.mr?.state === "paused") recording.mr.resume(); recState({ playing: true }); }
  function onEnded() { finishRecording("the video ended"); }

  // Start a segment on this element. Throws if Chrome refuses the capture
  // (cross-origin media, DRM): the panel then offers the whole-tab mode.
  function attach(el) {
    const r = recording;
    const stream = el.captureStream();
    r.capture = stream;       // also release tracks if setup throws halfway through
    // Every capture taps the element until its tracks are stopped, and a
    // tap left behind keeps the sound: the next capture of the same video —
    // a second recording, or the next segment — then gets silence. So the
    // picture (never needed: it only cost CPU at 1080p) stops right away,
    // and the sound when its segment ends (endSegment).
    for (const t of stream.getVideoTracks()) t.stop();
    const tracks = stream.getAudioTracks();
    if (!tracks.length) {
      // Nothing to capture until it plays; the tick tries again — with a
      // fresh capture, so this empty one mustn't linger.
      for (const t of stream.getTracks()) t.stop();
      r.el = el;
      return false;
    }
    const seg = r.seg++;
    const mr = new MediaRecorder(new MediaStream(tracks),
      { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: r.bitrate });
    mr.ondataavailable = (e) => queueChunk(r, seg, e.data);
    const segment = { mr, tracks, stream, done: null, closing: null };
    r.segment = segment;
    segment.done = new Promise((ok) => { mr.onstop = ok; });
    // A player that swaps its source ends the track: close this segment, and
    // the tick opens the next one on whatever then plays.
    for (const track of tracks) track.addEventListener("ended", () => {
      // An old track can finish after its replacement has started.
      if (recording === r && r.segment === segment) void endSegment(r);
    });
    mr.onerror = (event) => {
      if (recording !== r || r.segment !== segment) return;
      recState({ error: event.error?.message || "The audio recorder failed" });
      void finishRecording("recorder error");
    };
    mr.start(CHUNK_MS);
    if (el.paused) mr.pause();
    if (r.el && r.el !== el) detachEvents(r.el);
    r.el = el;
    r.mr = mr;
    for (const [e, fn] of [["pause", onPause], ["play", onPlay], ["ended", onEnded]]) el.addEventListener(e, fn);

    // The level meter: for "is anything coming through at all" and for the
    // silence stop. An AudioContext in a frame nobody clicked stays
    // suspended and reads zeros — then the meter is off, not "silent".
    r.ctx?.close().catch(() => {});
    r.ctx = new AudioContext();
    r.ctx.resume().catch(() => {});
    r.analyser = r.ctx.createAnalyser();
    r.ctx.createMediaStreamSource(new MediaStream(tracks)).connect(r.analyser);
    r.buf = new Float32Array(r.analyser.fftSize);
    recState({ segment: seg, playing: !el.paused });
    return true;
  }

  function detachEvents(el) {
    for (const [e, fn] of [["pause", onPause], ["play", onPlay], ["ended", onEnded]]) el.removeEventListener(e, fn);
  }

  // Stops the current segment and waits until its last chunk is queued.
  // Safe to call twice — and it is: when a video ends, Chrome ends the
  // captured track (-> here) a moment before "ended" fires (-> finish, -> here
  // again). The second call must still wait for that last chunk; returning
  // early because the recorder was already stopping lost the final seconds
  // of every recording.
  function endSegment(r = recording) {
    const segment = r?.segment;
    if (!segment) return Promise.resolve();
    if (segment.closing) return segment.closing;
    segment.closing = (async () => {
      if (segment.mr.state !== "inactive") segment.mr.stop();
      await segment.done; // includes the final dataavailable, even on a second call
      for (const track of segment.stream.getTracks()) track.stop();
      if (r.segment === segment) {
        r.segment = null;
        r.mr = null;
        r.capture = null;
      }
    })();
    return segment.closing;
  }

  function level() {
    const r = recording;
    if (!r?.analyser || r.ctx.state !== "running") return null;
    r.analyser.getFloatTimeDomainData(r.buf);
    let sum = 0;
    for (const v of r.buf) sum += v * v;
    return Math.sqrt(sum / r.buf.length);
  }

  async function tick() {
    const r = recording;
    if (!r || r.finishing || r.transitioning) return;
    // The player rebuilt its <video> (reconnect, quality switch): find what
    // plays now and carry on in a new segment. Recorded audio is untouched.
    if (!r.el || !r.el.isConnected || !r.mr || r.segment?.closing) {
      r.transitioning = true;
      await endSegment(r);
      if (recording !== r || r.finishing) { r.transitioning = false; return; }
      const live = r.el?.isConnected ? r.el : null;
      const next = live || collect(document).find((m) => m.tagName === r.tag && !m.paused)
        || collect(document).find((m) => m.tagName === r.tag);
      if (next && (!r.mr || next !== r.el)) {
        try {
          if (attach(next) && r.seg > 1) recState({ note: "The player was rebuilt — recording continued" });
        } catch (e) {
          recState({ error: `Can't record this player: ${e.message}` });
          finishRecording("capture refused");
          return;
        }
      }
      r.transitioning = false;
    }
    if (!r.mr || !r.el || r.el.paused || r.el.ended) return;

    const rms = level();
    if (rms == null) return;
    r.playingS++;
    if (rms > SILENT_RMS) { r.heard = true; r.silentS = 0; }
    else r.silentS++;
    if (!r.heard && r.playingS === UNHEARD_WARN_S) {
      recState({ warning: "No sound is coming through — try “Whole tab” instead" });
    }
    if (r.heard && r.silentS === 2) recState({ warning: null });
    const limit = r.el.duration === Infinity ? r.silenceStopLive : r.silenceStop;
    if (r.silentS >= limit) finishRecording(`${limit} s of silence`);
  }

  function beginRecording(msg) {
    if (recording) throw new Error("This frame is already recording");
    const el = find(msg.key);
    if (!el) throw new Error("That player is gone — rescan");
    if (el.mediaKeys) throw new Error("This video is DRM-protected: only “Whole tab” can record it");
    recording = {
      recId: msg.recId, tag: el.tagName, seg: msg.seg || 0, bitrate: msg.bitrate,
      silenceStop: msg.silenceStop, silenceStopLive: msg.silenceStopLive,
      queue: Promise.resolve(), segment: null, capture: null, el: null, mr: null, ctx: null,
      heard: false, silentS: 0, playingS: 0, finishing: false,
    };
    try {
      attach(el);
    } catch (e) {
      const r = recording;
      if (r.mr?.state !== "inactive" && r.mr) r.mr.stop();
      for (const track of r.capture?.getTracks() || []) track.stop();
      if (r.el) detachEvents(r.el);
      r.ctx?.close().catch(() => {});
      recording = null;
      throw new Error(/cross-origin/i.test(e.message)
        ? "Chrome blocks capturing this player (cross-origin media) — use “Whole tab”"
        : /EME|encrypted/i.test(e.message)
          ? "This video is DRM-protected: only “Whole tab” can record it"
          : e.message);
    }
    recording.timer = setInterval(tick, 1000);
  }

  async function finishRecording(reason) {
    const r = recording;
    if (!r || r.finishing) return;
    r.finishing = true;
    clearInterval(r.timer);
    await endSegment(r);
    await r.queue;                    // every chunk is on disk before "finished"
    if (r.el) detachEvents(r.el);
    for (const track of r.capture?.getTracks() || []) track.stop();
    await r.ctx?.close().catch(() => {});
    recording = null;
    try { await chrome.runtime.sendMessage({ type: "rec-finished", recId: r.recId, reason }); }
    catch { /* nobody to tell */ }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "rec-begin") {
      try { beginRecording(msg); sendResponse({ ok: true }); }
      catch (e) { sendResponse({ ok: false, error: e.message }); }
      return;
    }
    if (msg.type === "rec-end") {
      const mine = recording?.recId === msg.recId;
      if (mine) finishRecording(msg.reason || "stopped");
      sendResponse({ ok: mine });
      return;
    }
    if (msg.type === "rec-ping") {
      sendResponse({ ok: recording?.recId === msg.recId });
      return;
    }
    if (msg.type === "probe-stop") stop();
    else if (msg.type === "probe-flash") {
      const el = find(msg.key);
      if (el) flash(el);
    } else if (msg.type === "probe-reveal-frame" && window === window.top) {
      // The video is in a child frame: bring that <iframe> into view here.
      const frame = [...document.querySelectorAll("iframe, frame")]
        .find((f) => f.src && (f.src === msg.url || msg.url.startsWith(f.src.split("#")[0])));
      if (frame) flash(frame);
    }
  });

  // The frame is going away (navigation, iframe removed): its rows must too.
  addEventListener("pagehide", () => {
    if (running) send({ type: "probe", url: location.href, top: window === window.top, gone: true, sources: [] });
    // Whatever is still in the recorder can't be saved from here; the chunks
    // already sent are on disk, and background.js closes the recording.
    if (recording) send({ type: "rec-finished", recId: recording.recId, reason: "the page was left" });
  });

  window.__trProbe = { start, stop };
  start();
})();
