const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../extension/sidepanel.js'), 'utf8');
function harness() {
  let now = 100;
  const context = vm.createContext({ Date: {now: () => now * 1000} });
  vm.runInContext(source.slice(source.indexOf('const STAGE_LABEL ='), source.indexOf('function el(')), context);
  return {context, setTime: t => {now = t;}};
}

test('progress does not invent completed audio as the clock advances', () => {
  const h = harness();
  const p = {stage: 'whisper', phase: 'transcribing', reported_at: 100,
    fraction: .3, remaining: 20, low: 15, high: 30};
  assert.equal(h.context.estimate(p).frac, .3);
  h.setTime(105);
  const current = h.context.estimate(p);
  assert.equal(current.frac, .3);
  assert.equal(current.left, 15);
  assert.match(h.context.progressText(p), /30%.*sec left/);
});

test('loading has no percentage and an expired ETA is not a zero countdown', () => {
  const h = harness();
  assert.equal(h.context.progressText({phase: 'loading', stage: 'whisper', reported_at: 100,
    fraction: null, remaining: null}), 'loading Whisper model');
  h.setTime(200);
  const p = {stage: 'whisper', phase: 'transcribing', reported_at: 100,
    fraction: .7, remaining: 20, low: 15, high: 30};
  assert.equal(h.context.estimate(p).left, null);
  assert.equal(h.context.progressText(p), 'transcribing · 70%');
});

test('finishing never shows 100% or promises zero seconds', () => {
  const h = harness();
  const text = h.context.progressText({stage: 'whisper', phase: 'finishing', reported_at: 100,
    fraction: .99, remaining: null});
  assert.equal(text, 'finishing transcript · 99%');
});
