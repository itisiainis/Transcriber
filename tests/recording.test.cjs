// Run with node --test tests/recording.test.cjs. No browser/npm dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const flush = () => new Promise(setImmediate);
const extension = (name) => fs.readFileSync(path.join(__dirname, '../extension', name), 'utf8');

class Events {
  constructor() { this.events = new Map(); }
  addEventListener(name, fn) {
    if (!this.events.has(name)) this.events.set(name, new Set());
    this.events.get(name).add(fn);
  }
  removeEventListener(name, fn) { this.events.get(name)?.delete(fn); }
  emit(name, event = {}) { for (const fn of this.events.get(name) || []) fn(event); }
}

function mediaHarness() {
  const recorders = [], tracks = [], contexts = [], messages = [], intervals = new Set();
  class Track extends Events {
    constructor(kind = 'audio') { super(); this.kind = kind; this.stopped = false; tracks.push(this); }
    stop() { this.stopped = true; }
  }
  class Stream {
    constructor(items = [new Track()]) { this.items = items; }
    getTracks() { return this.items; }
    getAudioTracks() { return this.items.filter((t) => t.kind === 'audio'); }
    getVideoTracks() { return this.items.filter((t) => t.kind === 'video'); }
  }
  class Recorder {
    constructor(stream) { this.stream = stream; this.state = 'inactive'; recorders.push(this); }
    start() { this.state = 'recording'; }
    pause() { this.state = 'paused'; }
    resume() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      // Delayed on purpose: the next interval may run before the final chunk.
      this.flush = () => {
        this.ondataavailable?.({ data: new Blob(['audio']) });
        this.onstop?.();
      };
    }
  }
  class Context {
    constructor() { this.state = 'running'; this.destination = {}; contexts.push(this); }
    resume() { return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource() { return { connect() {} }; }
    createAnalyser() { return { fftSize: 32, getFloatTimeDomainData: (buf) => buf.fill(0.1) }; }
  }
  const listeners = [];
  const sandbox = {
    Blob, Uint8Array, Float32Array, AudioContext: Context, MediaStream: Stream, MediaRecorder: Recorder,
    btoa: (str) => Buffer.from(str, 'binary').toString('base64'),
    setInterval: (fn) => { intervals.add(fn); return fn; }, clearInterval: (fn) => intervals.delete(fn),
    setTimeout, clearTimeout,
    chrome: { runtime: {
      sendMessage: async (msg) => { messages.push(msg); return { ok: true }; },
      onMessage: { addListener: (fn) => listeners.push(fn) },
    } },
  };
  const context = vm.createContext(sandbox);
  const send = (msg) => new Promise((resolve) => {
    for (const listener of listeners) listener(msg, {}, resolve);
  });
  return { sandbox, context, send, recorders, tracks, contexts, messages, intervals, Stream, Track };
}

function probeHarness() {
  const h = mediaHarness();
  let elements = [], observations = 0;
  class Observer {
    observe() { observations++; }
    disconnect() {}
  }
  const document = { title: 'Lesson', querySelectorAll: (selector) => selector === 'video, audio' ? elements : [] };
  const window = {}; window.top = window;
  Object.assign(h.sandbox, { window, document, MutationObserver: Observer,
    location: { href: 'https://example.com' }, addEventListener() {} });
  vm.runInContext(extension('record-probe.js'), h.context);
  function player() {
    const el = Object.assign(new Events(), { tagName: 'VIDEO', paused: false, ended: false,
      isConnected: true, duration: 60, volume: 1, currentSrc: 'https://example.com/a.webm',
      getBoundingClientRect: () => ({ width: 640, height: 480 }),
      captureStream: () => new h.Stream([new h.Track(), new h.Track('video')]),
    });
    elements.push(el);
    h.sandbox.window.__trProbe.start();
    return el;
  }
  return { ...h, player, observations: () => observations };
}

test('same player records twice without a page refresh; each stop flushes before releasing tracks', async () => {
  const h = probeHarness(); h.player();
  for (const recId of ['first', 'second']) {
    assert.equal((await h.send({ type: 'rec-begin', recId, key: 'm1', bitrate: 32000 })).ok, true);
    const mr = h.recorders.at(-1), audio = mr.stream.getAudioTracks()[0];
    await h.send({ type: 'rec-end', recId });
    assert.equal(audio.stopped, false);
    assert.equal(h.messages.filter((m) => m.type === 'rec-finished' && m.recId === recId).length, 0);
    mr.flush(); await flush();
    assert.equal(audio.stopped, true);
    const sent = h.messages.filter((m) => m.recId === recId).map((m) => m.type);
    assert.ok(sent.indexOf('rec-chunk') < sent.indexOf('rec-finished'));
    assert.ok(h.tracks.every((t) => t.stopped));
    assert.ok(h.contexts.every((c) => c.state === 'closed'));
  }
});

test('player replacement closes the old recorder; late old-track events cannot stop the new one', async () => {
  const h = probeHarness(); const first = h.player();
  await h.send({ type: 'rec-begin', recId: 'replace', key: 'm1', bitrate: 32000 });
  const old = h.recorders[0], oldTrack = old.stream.getAudioTracks()[0];
  first.isConnected = false; first.paused = true; h.player();
  const tick = [...h.intervals].at(-1);
  const pending = tick();
  assert.equal(old.state, 'inactive');
  assert.equal(h.recorders.length, 1);
  await tick();                       // overlapping tick must not attach early
  old.flush(); await pending;
  assert.equal(h.recorders.length, 2);
  oldTrack.emit('ended'); await flush();
  assert.equal(h.recorders[1].state, 'recording');
  await h.send({ type: 'rec-end', recId: 'replace' });
  h.recorders[1].flush(); await flush();
  assert.ok(h.tracks.every((t) => t.stopped));
  assert.deepEqual(h.messages.filter((m) => m.type === 'rec-chunk').map((m) => m.seg), [0, 1]);
});

test('simultaneous track end and Stop still wait for the final chunk', async () => {
  const h = probeHarness(); h.player();
  await h.send({ type: 'rec-begin', recId: 'end', key: 'm1', bitrate: 32000 });
  h.recorders[0].stream.getAudioTracks()[0].emit('ended');
  await h.send({ type: 'rec-end', recId: 'end' });
  assert.equal(h.messages.some((m) => m.type === 'rec-finished'), false);
  h.recorders[0].flush(); await flush();
  assert.equal(h.messages.at(-1).type, 'rec-finished');
  assert.ok(h.tracks.every((t) => t.stopped));
});

test('probe observers reconnect after closing and reopening the Record screen', () => {
  const h = probeHarness();
  h.sandbox.window.__trProbe.stop();
  h.sandbox.window.__trProbe.start();
  assert.equal(h.observations(), 2);
});

test('whole-tab recorder flushes, releases playback and can immediately record again', async () => {
  const h = mediaHarness();
  h.sandbox.navigator = { mediaDevices: { getUserMedia: async () => new h.Stream() } };
  vm.runInContext(extension('record-tab.js'), h.context);
  for (const recId of ['tab-first', 'tab-second']) {
    const msg = { target: 'tab-recorder', recId };
    assert.equal((await h.send({ ...msg, type: 'rec-begin', streamId: 'id', bitrate: 32000 })).ok, true);
    assert.equal((await h.send({ ...msg, type: 'rec-end' })).ok, true);
    h.recorders.at(-1).flush(); await flush();
    const sent = h.messages.filter((m) => m.recId === recId).map((m) => m.type);
    assert.deepEqual(sent, ['rec-chunk', 'rec-finished']);
    assert.ok(h.tracks.every((t) => t.stopped));
    assert.ok(h.contexts.every((c) => c.state === 'closed'));
  }
});

test('whole-tab setup failure releases tracks and permits retry', async () => {
  const h = mediaHarness();
  h.sandbox.navigator = { mediaDevices: { getUserMedia: async () => new h.Stream() } };
  h.sandbox.MediaRecorder = class { constructor() { throw new Error('unsupported'); } };
  vm.runInContext(extension('record-tab.js'), h.context);
  const msg = { target: 'tab-recorder', type: 'rec-begin', recId: 'failed' };
  assert.equal((await h.send(msg)).error, 'unsupported');
  assert.ok(h.tracks.every((t) => t.stopped));
  h.sandbox.MediaRecorder = mediaHarness().sandbox.MediaRecorder;
  assert.equal((await h.send(msg)).ok, true);
});

test('Stop during tab capture startup waits for the stream, then flushes it', async () => {
  const h = mediaHarness();
  let supplyStream;
  h.sandbox.navigator = { mediaDevices: { getUserMedia: () => new Promise((resolve) => { supplyStream = resolve; }) } };
  vm.runInContext(extension('record-tab.js'), h.context);
  const msg = { target: 'tab-recorder', recId: 'starting' };
  const started = h.send({ ...msg, type: 'rec-begin' });
  assert.equal((await h.send({ ...msg, type: 'rec-end', reason: 'stopped' })).ok, true);
  supplyStream(new h.Stream());
  assert.equal((await started).ok, true);
  assert.equal(h.recorders[0].state, 'inactive');
  h.recorders[0].flush(); await flush();
  assert.equal(h.messages.at(-1).type, 'rec-finished');
  assert.ok(h.tracks.every((t) => t.stopped));
});

function backgroundHarness() {
  const listeners = [], updates = [], removed = [], requests = [], tabMessages = [], offscreen = [];
  let hostReply, count = 0, saveReply;
  const event = () => ({ addListener() {} });
  const sandbox = {
    console, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    chrome: {
      runtime: {
        onInstalled: event(), onStartup: event(),
        onMessage: { addListener: (fn) => listeners.push(fn) },
        getContexts: async () => [],
        connectNative: () => ({
          onMessage: { addListener: (fn) => { hostReply = fn; } }, onDisconnect: event(), disconnect() {},
          postMessage(msg) {
            requests.push(msg);
            const result = { id: msg.id, ok: true };
            if (msg.cmd === 'rec_start') Object.assign(result, { rec_id: `r${++count}`, bitrate: 32000 });
            if (msg.cmd === 'rec_stop') { saveReply = () => hostReply({ ...result, duration: 10 }); return; }
            queueMicrotask(() => hostReply(result));
          },
        }),
        sendMessage: async (msg) => { offscreen.push(msg); return { ok: true }; },
      },
      tabs: {
        sendMessage: async (...args) => { tabMessages.push(args); return { ok: true }; },
        onRemoved: { addListener: (fn) => removed.push(fn) },
        onUpdated: { addListener: (fn) => updates.push(fn) },
      },
      tabCapture: { getMediaStreamId: async () => 'stream' },
      offscreen: { createDocument: async () => {}, closeDocument: async () => {} },
      storage: { session: { get: async () => ({}), set() {}, remove() {} } },
      sidePanel: { setPanelBehavior() {} },
      notifications: { onClicked: event() },
      action: { setBadgeText() {}, setTitle() {}, setBadgeBackgroundColor() {}, setBadgeTextColor() {} },
    },
  };
  vm.runInNewContext(extension('background.js'), sandbox);
  const send = (msg) => new Promise((resolve) => {
    for (const listener of listeners) listener(msg, {}, resolve);
  });
  return { send, requests, updates, removed, tabMessages, offscreen, save: () => saveReply() };
}

test('background rejects simultaneous starts and a new start/dismiss while saving', async () => {
  const h = backgroundHarness();
  const msg = { type: 'rec-start', tabId: 1, frameId: 0, mode: 'element' };
  const [first, second] = await Promise.all([h.send(msg), h.send(msg)]);
  assert.equal(first.ok, true); assert.equal(second.ok, false);
  assert.equal(h.requests.filter((m) => m.cmd === 'rec_start').length, 1);
  const finished = h.send({ type: 'rec-finished', recId: 'r1', reason: 'stopped' });
  await flush();
  assert.equal((await h.send(msg)).ok, false);
  await h.send({ type: 'rec-dismiss' });
  assert.equal((await h.send({ type: 'rec-status' })).recording.status, 'saving');
  h.save(); await finished;
  assert.equal((await h.send(msg)).ok, true);
});

test('whole-tab navigation leaves recording active; tab closure asks the recorder to flush', async () => {
  const h = backgroundHarness();
  assert.equal((await h.send({ type: 'rec-start', mode: 'tab', tabId: 7 })).ok, true);
  assert.equal(h.offscreen.find((m) => m.type === 'rec-begin').streamId, 'stream');
  h.updates[0](7, { status: 'loading' });
  await flush();
  assert.equal(h.requests.some((m) => m.cmd === 'rec_stop'), false);
  h.removed[0](7); await flush();
  assert.ok(h.offscreen.some((m) => m.type === 'rec-end' && m.target === 'tab-recorder'));
  assert.equal(h.requests.some((m) => m.cmd === 'rec_stop'), false); // wait for rec-finished
});
