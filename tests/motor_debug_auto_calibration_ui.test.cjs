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
    WZ: wizard, $: selector => nodes[selector.slice(1)],
    confirm: () => accept, toast: (...args) => messages.push(args),
    wzAutoOpen: () => {}, setBusy: () => {}, refreshWizard: () => {},
    wz: async (action, body) => { calls.push({action, body}); return {runId: "run-1"}; },
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
