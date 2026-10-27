const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../scripts/motor_debug_page.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mapping = script.slice(script.indexOf('let cameraMappingPending'),
  script.indexOf('$("#deviceId").addEventListener("change"'));
const reuse = script.slice(script.indexOf('function renderLayoutReuse()'),
  script.indexOf('function renderWizard()'));

function harness({accept = true} = {}) {
  const nodes = new Map(), handlers = {}, calls = [], actions = [];
  let device = 'arm-01';
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {value: '', textContent: '', disabled: false,
      children: [], style: {}, replaceChildren() {this.children = [];},
      appendChild(child) {this.children.push(child);},
      addEventListener(type, fn) {handlers[selector] = fn;}});
    return nodes.get(selector);
  };
  const context = {$: node, DEV_RAW: () => device, selectionGeneration: 1,
    document: {createElement: () => ({})}, confirm: () => accept, toast: () => {},
    crypto: {randomUUID: () => 'request-1'},
    closeDeviceContext: async () => actions.push('close'),
    location: {reload: () => actions.push('reload')},
    api: (url, init) => new Promise((resolve, reject) => calls.push({url, init, resolve, reject})),
    WZ: null};
  vm.runInNewContext(mapping + reuse, context);
  return {node, handlers, calls, actions, context, switchDevice() {
    device = 'arm-02'; context.selectionGeneration++;
  }};
}

test('channel options show independent numbered channels and drop stale device replies', async () => {
  new vm.Script(script);
  const h = harness();
  const first = h.handlers['#btnCameraOptions']();
  h.calls[0].resolve({camera_options: [
    {resource_id: 'camera:cam-09', label: 'cam-09', selected: true},
    {resource_id: 'untrusted', label: 'ignored'}]});
  await first;
  assert.equal(h.node('#cameraChannelSel').children.length, 1);
  assert.equal(h.node('#cameraChannelSel').children[0].value, 'camera:cam-09');
  const second = h.handlers['#btnCameraOptions']();
  h.switchDevice();
  h.node('#cameraChannelSel').replaceChildren();
  h.calls[1].resolve({camera_options: [{resource_id: 'camera:cam-01'}]});
  await second;
  assert.equal(h.node('#cameraChannelSel').children.length, 0);
});

test('cancelled confirmation sends no mapping request', async () => {
  const h = harness({accept: false});
  h.node('#cameraChannelSel').value = 'camera:cam-09';
  await h.handlers['#btnBindCamera']();
  assert.equal(h.calls.length, 0);
});

test('mapping saves once and reconstructs camera context after success', async () => {
  const h = harness();
  h.node('#cameraChannelSel').value = 'camera:cam-09';
  const first = h.handlers['#btnBindCamera']();
  await h.handlers['#btnBindCamera']();
  assert.equal(h.calls.length, 1);
  assert.equal(h.node('#deviceId').disabled, true);
  assert.deepEqual(JSON.parse(h.calls[0].init.body), {
    device: 'arm-01', cameraResource: 'camera:cam-09', requestId: 'request-1'});
  h.calls[0].resolve({});
  await first;
  assert.deepEqual(h.actions, ['close', 'reload']);
});

test('mapping failure remains visible without retry or page reload', async () => {
  const h = harness();
  h.node('#cameraChannelSel').value = 'camera:cam-09';
  const first = h.handlers['#btnBindCamera']();
  h.calls[0].reject(new Error('409: 该通道正在使用'));
  await first;
  assert.match(h.node('#cameraChannelMsg').textContent, /保存失败.*409/);
  assert.equal(h.node('#deviceId').disabled, false);
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.actions, []);
});

test('saved calibration displays layout reuse warnings and clears stale warnings', () => {
  const h = harness();
  h.context.WZ = {layoutReuse: {status: 'warning', message: '新校准已保存，页面资料复用失败，请核对'}};
  h.context.renderLayoutReuse();
  assert.match(h.node('#wzLayoutReuse').textContent, /⚠.*新校准已保存/);
  assert.equal(h.node('#wzLayoutReuse').style.color, '#ffce8a');
  h.context.WZ = {auto: {layoutReuse: {status: 'ready', message: '页面资料已复用'}}};
  h.context.renderLayoutReuse();
  assert.equal(h.node('#wzLayoutReuse').textContent, '页面资料已复用');
  assert.equal(h.node('#wzLayoutReuse').style.color, '');
  h.context.WZ = {};
  h.context.renderLayoutReuse();
  assert.equal(h.node('#wzLayoutReuse').style.display, 'none');
});
