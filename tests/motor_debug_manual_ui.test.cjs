const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../scripts/motor_debug_page.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const motion = script.slice(script.indexOf('async function motion('), script.indexOf('// ---------- 状态轮询'));

function harness() {
  const calls = [], notices = [];
  const context = {S: {busy: 0}, REGION: {framing: true},
    api: async path => {calls.push(path); return {mpos: {x: 1, y: 2, z: 3}};},
    toast: (message) => notices.push(message), setBusy: () => {},
    renderMpos: () => {}, refreshStatus: () => {}};
  vm.runInNewContext(motion, context);
  return {calls, notices, context};
}

for (const path of ['/api/jog', '/api/tap', '/api/hover', '/api/resume']) {
  test(`manual ${path} is independent of region editing`, async () => {
    const h = harness();
    await h.context.motion(path, {}, '', '手动');
    assert.deepEqual(h.calls, [path]);
    assert.equal(h.notices.length, 0);
  });
}

test('automatic gesture still waits for region editing to finish', async () => {
  const h = harness();
  await h.context.motion('/api/gestures/test', {}, '', '手势');
  assert.deepEqual(h.calls, []);
  assert.match(h.notices[0], /框选/);
});
