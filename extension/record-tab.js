// An offscreen document owns tab audio so panel closure, player replacement,
// silence and navigation do not interrupt a recording. Stop is explicit.
let recording = null;

function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(text);
}

async function begin(msg) {
  if (recording) throw new Error("Already recording this tab");
  const r = { recId: msg.recId, queue: Promise.resolve(), stream: null, ctx: null, mr: null };
  recording = r;
  try {
    r.stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: msg.streamId } },
      video: false,
    });
    // tabCapture diverts playback; route it back to the speakers.
    r.ctx = new AudioContext();
    r.ctx.createMediaStreamSource(r.stream).connect(r.ctx.destination);
    await r.ctx.resume();
    r.mr = new MediaRecorder(r.stream, { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: msg.bitrate });
    r.done = new Promise((resolve) => { r.mr.onstop = resolve; });
    r.mr.ondataavailable = ({ data }) => {
      r.queue = r.queue.then(async () => {
        if (!data.size) return;
        const res = await chrome.runtime.sendMessage({
          type: "rec-chunk", recId: r.recId, seg: 0, data: base64(await data.arrayBuffer()),
        });
        if (!res?.ok) throw new Error(res?.error || "The audio chunk was not saved");
      }).catch((error) => {
        r.error = error.message;
        chrome.runtime.sendMessage({ type: "rec-state", recId: r.recId, error: error.message }).catch(() => {});
        void finish("audio could not be saved");
      });
    };
    r.mr.onerror = (event) => {
      r.error = event.error?.message || "The audio recorder failed";
      void finish("recorder error");
    };
    for (const track of r.stream.getTracks()) track.addEventListener("ended", () => void finish("tab capture ended"));
    r.mr.start(10000);
    if (r.stopReason) void finish(r.stopReason);
  } catch (error) {
    for (const track of r.stream?.getTracks() || []) track.stop();
    await r.ctx?.close().catch(() => {});
    recording = null;
    throw error;
  }
}

async function finish(reason) {
  const r = recording;
  if (!r || r.finishing) return;
  if (!r.mr || !r.done) { r.stopReason = reason; return; }
  r.finishing = true;
  if (r.mr?.state !== "inactive") r.mr.stop();
  await r.done;                         // final dataavailable precedes stop
  await r.queue;
  for (const track of r.stream.getTracks()) track.stop();
  await r.ctx.close().catch(() => {});
  recording = null;
  await chrome.runtime.sendMessage({ type: "rec-finished", recId: r.recId, reason, error: r.error }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.target !== "tab-recorder") return;
  if (msg.type === "rec-begin") {
    begin(msg).then(() => reply({ ok: true }), (error) => reply({ ok: false, error: error.message }));
    return true;
  }
  if (msg.type === "rec-end") {
    const mine = recording?.recId === msg.recId;
    if (mine) void finish(msg.reason || "stopped");
    reply({ ok: mine });
  } else if (msg.type === "rec-ping") {
    reply({ ok: recording?.recId === msg.recId });
  }
});
