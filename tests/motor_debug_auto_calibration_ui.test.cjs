const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const html = fs.readFileSync(
  path.join(__dirname, "../scripts/motor_debug_page.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const handlers = script.slice(
  script.indexOf('$("#btnWzAutoStart").addEventListener'),
  script.indexOf('$("#btnWzAutoStop").addEventListener'));

function harness({confirmed = true, accept = true} = {}) {
  const events = {};
  const calls = [];
  const messages = [];
  const nodes = {
    btnWzAutoStart: {addEventListener: (type, fn) => { events.start = fn; }},
  };
  const wizard = {auto: {runId: "run-1", confirmations: confirmed
    ? {zSafe: {validated: true}, safePark: {confirmed: true}} : {}}};
  const context = {
    S: {busy: 0}, WZ: wizard, $: selector => nodes[selector.slice(1)],
    confirm: () => accept, toast: (...args) => messages.push(args),
    wzAutoOpen: () => {}, setBusy: () => {}, refreshWizard: () => {},
    wzAction: async (action, body) => { calls.push({action, body}); return {runId: "run-1"}; },
  };
  vm.runInNewContext(handlers, context);
  return {events, calls, messages, wizard};
}

test("automatic calibration saves READY without any input-acceptance controls", async () => {
  assert.doesNotMatch(html, /wzAutoDeferInput|wzAutoMotionResume|wzAutoAcceptance|输入法验收目标/);
  assert.match(html, /五点独立验证通过并安全停车后自动保存 READY/);
  const autoControls = html.slice(html.indexOf('id="wzAUTO"'), html.indexOf('id="wzAutoFailure"'));
  // 开关已删（2026-09-30 拍板）：PROMOTED 即可用，页面不再有下发任务开关
  assert.doesNotMatch(autoControls, /autoGSettingNavigation|下发任务（自动导航）/);
  assert.match(autoControls, /开关已删除/);
  new vm.Script(script);
  const h = harness();
  await h.events.start();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].action, "auto-start");
  assert.equal(JSON.stringify(h.calls[0].body), "{}");
});

test("operator confirmations and cancellation still prevent motion", async () => {
  const unconfirmed = harness({confirmed: false});
  await unconfirmed.events.start();
  assert.equal(unconfirmed.calls.length, 0);
  const cancelled = harness({accept: false});
  await cancelled.events.start();
  assert.equal(cancelled.calls.length, 0);
});

// Run the actual request function and click handlers with a deferred HTTP reply:
// a second click while the first contact is pending must not queue another move.
function pendingHarness() {
  const events = {};
  const calls = [];
  const messages = [];
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {disabled: false, value: '8',
      textContent: '', style: {}, children: [], appendChild(child) {this.children.push(child);}, addEventListener: (type, handler) => events[selector] = handler});
    return nodes.get(selector);
  };
  node('#wzAutoTapFeed').value = '2000';
  node('#wzAutoTapDwell').value = '0';
  const state = {busy: 0, contextId: 'ctx-1'};
  const context = {
    document: {createElement: () => ({innerHTML: ''})},
    S: state, $: node, $$: selectors => selectors.split(',').map(s => node(s.trim())),
    DEV_RAW: () => 'arm-01', DEBUG_WRITE_HEADERS: () => ({}),
    confirm: () => true, toast: (...args) => messages.push(args),
    wzAutoOpen: () => {}, refreshWizard: async () => {},
    renderBasic: () => {}, renderMpos: () => {}, refreshStatus: () => {},
    renderGesture: () => {}, renderAutoGestures: () => {}, renderUITargets: () => {},
    api: async (url, init) => {calls.push({url, body: JSON.parse(init.body || '{}')}); return {};},
    fetch: (url, init) => new Promise((resolve, reject) => {
      calls.push({url, body: JSON.parse(init.body), resolve: data => resolve({ok: true, json: async () => data}), reject});
    }),
  };
  const requestCode = script.slice(script.indexOf('let WZ = null;'), script.indexOf('function renderWizard()'));
  const safeHandler = script.slice(script.indexOf('$("#wzAutoTapDwell").addEventListener'),
    script.indexOf('$("#btnWzAutoPark").addEventListener'));
  const tapHandler = script.slice(script.indexOf('$("#btnWzTapTest").addEventListener'),
    script.indexOf('$("#btnWzTapSave").addEventListener'));
  const diagnosticTap = script.slice(script.indexOf('$("#btnTap").addEventListener'),
    script.indexOf('$("#btnHover").addEventListener'));
  const motionCode = script.slice(script.indexOf('function setBusy(delta)'), script.indexOf('// ---------- 状态轮询'));
  const emergencyHandler = script.slice(script.indexOf('$("#btnEstop").addEventListener'),
    script.indexOf('$("#btnEstopReset").addEventListener'));
  const autoRenderCode = script.slice(script.indexOf('const AUTO_STATE_TEXT'),
    script.indexOf('$("#btnWzAutoZSafe").addEventListener'));
  vm.runInNewContext(motionCode + requestCode + autoRenderCode + safeHandler + tapHandler + diagnosticTap + handlers + emergencyHandler + `
    globalThis.setWizard = value => { WZ = value; };
    globalThis.redrawBusy = renderWizardBusy;
  `, context);
  return {events, calls, messages, nodes, node, state, context};
}

const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

test('safe Z confirmation sends one contact request while pending, then unlocks', async () => {
  const h = pendingHarness();
  h.events['#btnWzAutoZSafe']();
  h.events['#btnWzAutoZSafe']();
  assert.equal(h.calls.length, 1, 'repeated clicks must not issue a second contact');
  assert.equal(h.node('#btnWzAutoZSafe').disabled, true);
  assert.match(h.node('#wzActionInfo').textContent, /等待.*触点|触点.*等待/);
  h.calls[0].resolve({zSafe: -1, tapDepth: 8, tapFeed: 2000, report: {screenX: 500, screenY: 1000}});
  await flush();
  assert.equal(h.node('#btnWzAutoZSafe').disabled, false);
  assert.equal(h.state.busy, 0);
  assert.match(h.node('#wzActionInfo').textContent, /参数已验证/);
});

test('automatic confirmation carries the chosen dwell in milliseconds once', async () => {
  const h = pendingHarness();
  h.node('#wzAutoTapDwell').value = '100';
  h.events['#wzAutoTapDwell']();
  h.events['#btnWzAutoZSafe']();
  h.events['#btnWzAutoZSafe']();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.tapDwellMs, 100);
  assert.match(html, /额外停留.*实际触屏时长/);
  assert.match(html, /下压.*抬起.*时间/);
  assert.match(html, /重新开始校准/);
});

test('dwell hydration preserves saved settings and does not overwrite operator edits', () => {
  const h = pendingHarness();
  h.context.setWizard({z: {tap: {dwellMs: 120}}, bundle: {z: {tapDwellMs: 120}}, auto: {}});
  h.context.renderAuto();
  assert.equal(h.node('#wzAutoTapDwell').value, '120');
  h.node('#wzAutoTapDwell').value = '50';
  h.events['#wzAutoTapDwell']();
  h.context.renderAuto();
  assert.equal(h.node('#wzAutoTapDwell').value, '50');
});

test('diagnostic tap converts milliseconds once without saving formal parameters', async () => {
  const h = pendingHarness();
  h.node('#tapDwell').value = '50';
  h.node('#tapFeed').value = '2000';
  h.events['#btnTap']();
  h.events['#btnTap']();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, '/api/tap');
  assert.equal(h.calls[0].body.dwell, .05);
  await flush();
  assert.equal(h.calls.length, 1);
});

test('invalid dwell never sends either diagnostic or formal confirmation', () => {
  for (const value of ['-1', '2001', '1.5', 'invalid']) {
    const h = pendingHarness();
    h.node('#tapDwell').value = value;
    h.node('#wzAutoTapDwell').value = value;
    h.events['#btnTap']();
    h.events['#btnWzAutoZSafe']();
    assert.equal(h.calls.length, 0);
  }
});

test('failed confirmation is visible and allows one deliberate retry', async () => {
  const h = pendingHarness();
  h.events['#btnWzAutoZSafe']();
  h.calls[0].reject(new Error('未收到触点回报'));
  await flush();
  assert.equal(h.state.busy, 0);
  assert.equal(h.node('#btnWzAutoZSafe').disabled, false);
  assert.match(h.node('#wzActionInfo').textContent, /未收到触点回报/);
  h.events['#btnWzAutoZSafe']();
  assert.equal(h.calls.length, 2);
});

test('legacy ordinary tap test also sends one request while pending', async () => {
  const h = pendingHarness();
  h.events['#btnWzTapTest']();
  h.events['#btnWzTapTest']();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.action, 'z-test');
});

test('an unvalidated Z confirmation never starts automatic calibration', async () => {
  const h = harness();
  h.wizard.auto.confirmations.zSafe.validated = false;
  await h.events.start();
  assert.equal(h.calls.length, 0);
});


test('poll redraw cannot unlock a pending test or allow automatic start', async () => {
  const h = pendingHarness();
  h.context.setWizard({auto: {confirmations: {
    zSafe: {validated: true}, safePark: {confirmed: true},
  }}});
  h.events['#btnWzAutoZSafe']();
  h.context.redrawBusy();
  assert.equal(h.node('#btnWzAutoStart').disabled, true);
  await h.events['#btnWzAutoStart']();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.action, 'z-confirm-safe');
});

test('automatic start is also single flight until its HTTP reply', async () => {
  const h = pendingHarness();
  h.context.setWizard({auto: {confirmations: {
    zSafe: {validated: true}, safePark: {confirmed: true},
  }}});
  const first = h.events['#btnWzAutoStart']();
  const second = h.events['#btnWzAutoStart']();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.action, 'auto-start');
  h.calls[0].resolve({runId: 'run-2'});
  await Promise.all([first, second]);
  assert.equal(h.state.busy, 0);
  assert.match(h.node('#wzActionInfo').textContent, /自动校准已启动.*run-2/);
});


test('pending contact blocks manual motion and park while emergency stop remains available', async () => {
  const h = pendingHarness();
  h.events['#btnWzAutoZSafe']();
  h.context.redrawBusy();
  assert.equal(h.node('.jog').disabled, true);
  assert.equal(h.node('#btnPark').disabled, true);
  assert.equal(h.node('#btnOcrTap').disabled, true);
  await h.context.motion('/api/jog', {axis: 'x', delta: 1, feed: 2000});
  await h.context.motion('/api/park', {device: 'arm-01'});
  assert.equal(h.calls.length, 1, 'no manual move may interrupt the contact test');
  assert.equal(h.node('#btnEstop').disabled, false);
  await h.events['#btnEstop']();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].url, '/api/estop');
});


test('past auto-calibration failure does not obscure newly confirmed preparation', () => {
  const h = pendingHarness();
  h.context.setWizard({auto: {runId: 'old-run', state: 'FAILED',
    confirmations: {zSafe: {validated: true, zSafe: -1, tapDepth: 8, tapFeed: 2000},
      safePark: {confirmed: true, parkX: 0, parkY: 0, parkZ: -1}},
    lastFailure: {code: 'arm_quality_below_target', message: 'old model failed'}}});
  h.context.renderAuto();
  assert.match(h.node('#wzAutoNextStep').textContent, /准备已完成.*启动自动校准/);
  assert.match(h.node('#wzAutoBadge').innerHTML, /上次.*失败/);
  assert.match(h.node('#wzAutoFailure').textContent, /上次自动校准.*old-run/);
});


test('region acceptance shows measured target inclusion instead of a universal pixel gate', () => {
  const h = pendingHarness();
  h.context.setWizard({bundle: {validation: {criterion: 'target_region_v1'}},
    auto: {runId: 'region-run', validationCriterion: 'target_region_v1',
      thresholds: {target_tolerance_px: 10, arm_rms_target_px: 3},
      recentSamples: [{phase: 'VALIDATION', targetId: 'validation-1',
        errorLevel: 'SOFT_FAIL', touchTargetErrorPx: 30, eligible: true,
        regionEvidence: {criterion: 'target_region_v1', passed: true, marginPx: 11.25,
          safetyInsetPx: 5}}]}});
  h.context.renderAuto();
  assert.match(h.node('#wzAutoCriterion').textContent, /本轮记录：按目标范围验收/);
  assert.match(h.node('#wzAutoCriterion').textContent, /当前档案：按目标范围验收/);
  assert.match(h.node('#wzAutoThresholds').textContent, /短边.*5%/);
  assert.doesNotMatch(h.node('#wzAutoThresholds').textContent, /PASS≤10|Arm RMS≤3/);
  assert.match(h.node('#wzAutoSampleRows').children[0].innerHTML, /区域命中.*11.25/);
});

test('old calibration evidence remains labelled as legacy center precision', () => {
  const h = pendingHarness();
  h.context.setWizard({bundle: {validation: {passed: true}}, auto: {runId: 'old-run',
    thresholds: {target_tolerance_px: 10, arm_rms_target_px: 3}}});
  h.context.renderAuto();
  assert.match(h.node('#wzAutoCriterion').textContent, /本轮记录：中心精度验收（旧模式）/);
  assert.match(h.node('#wzAutoCriterion').textContent, /当前档案：中心精度验收（旧模式）/);
  assert.match(h.node('#wzAutoThresholds').textContent, /旧模式.*PASS≤10/);
});

test('region run never infers target inclusion from an old PASS or absent evidence', () => {
  const h = pendingHarness();
  h.context.setWizard({auto: {runId: 'new-run', validationCriterion: 'target_region_v1',
    recentSamples: [{targetId: 'one', errorLevel: 'PASS', eligible: true}]}});
  h.context.renderAuto();
  assert.match(h.node('#wzAutoSampleRows').children[0].innerHTML, /尚无区域验收证据/);
  assert.doesNotMatch(h.node('#wzAutoSampleRows').children[0].innerHTML, /✅/);
});
