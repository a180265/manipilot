const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../scripts/motor_debug_page.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const serial = script.slice(script.indexOf('// ---------- 串口换口检测'), script.indexOf('async function refreshCameraChannels'));

function harness(ports, respond) {
  const nodes = new Map(), handlers = {}, calls = [], notices = [];
  const ready = {daemon: {connected: true, session: 'new', device_present: true},
    mpos: {x: 1, y: 2, z: 3, state: 'Idle'}};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: '', children: [], style: {}, dataset: {},
      textContent: '', disabled: false, classList: {add() {}, remove() {}},
      set innerHTML(v) { this.children = []; },
      get selectedOptions() {return this.children.filter(p => p.value === this.value);},
      appendChild(p) {this.children.push(p); if (p.selected || !this.value) this.value = p.value;},
      addEventListener(event, fn) {handlers[id] = fn;}});
    return nodes.get(id);
  };
  let device = 'arm-01';
  const context = {$: node, $$: selector => selector.split(',').map(node),
    DEV_RAW: () => device, selectionGeneration: 1, S: {busy: 0},
    toast(message, kind) {notices.push({message, kind});},
    setBadge(node, _kind, text) {node.textContent = text;},
    renderBasic() {}, renderWizardBusy() {}, renderMpos() {}, redrawCamera() {},
    setTimeout: fn => {queueMicrotask(fn);},
    document: {createElement: () => ({dataset: {}})},
    api: async (url, opts) => {calls.push([url, opts]);
      return respond ? respond(url, opts) : url === '/api/connect'
        ? {connected: true, session: 'new'} : url.startsWith('/api/status') ? ready : {ports};},
    refreshArmDevices: async () => {}, refreshStatus: async () => context.renderStatus({
      daemon: {connected: false, session: null, device_present: true}})};
  vm.runInNewContext(serial, context);
  vm.runInNewContext(script.slice(script.indexOf('function setBusy('),
    script.indexOf('// ---------- 状态轮询')), context);
  vm.runInNewContext(script.slice(script.indexOf('function renderStatus('),
    script.indexOf('async function refreshStatus()')), context);
  vm.runInNewContext(script.slice(script.indexOf('async function connectSelectedDevice()'),
    script.indexOf('$("#btnDisconnect").addEventListener')), context);
  return {node, handlers, calls, notices, context, ready, selectDevice(value) {device = value;}};
}
const event = {preventDefault() {}};

test('own bound USB port stays connectable and is preferred to system ports', async () => {
  const h = harness([{path: '/dev/cu.Bluetooth', bound_device: null},
    {path: '/dev/cu.usbserial-130', bound_device: 'arm-01', state: 'present'}]);
  await h.handlers['#btnScanSerial'](event);
  assert.equal(h.node('#serialPortSel').value, '/dev/cu.usbserial-130');
  assert.notEqual(h.node('#btnBindSerial').style.display, 'none');
  await h.handlers['#btnBindSerial'](event);
  assert.equal(h.calls.filter(([url]) => url === '/api/connect').length, 1);
  assert.equal(h.calls.filter(([url]) => url === '/api/serial-bind').length, 0);
});

test('port bound to another arm stays unavailable', async () => {
  const h = harness([{path: '/dev/cu.usbserial-2', bound_device: 'arm-02'}]);
  await h.handlers['#btnScanSerial'](event);
  assert.equal(h.node('#btnBindSerial').style.display, 'none');
  await h.handlers['#btnBindSerial'](event);
  assert.equal(h.calls.length, 1);
});


test('binding blocks both connection entry points until save completes', async () => {
  let finish;
  const h = harness([], (url) => url.startsWith('/api/serial-ports')
    ? {ports: [{path: '/dev/cu.usbserial-new'}]}
    : new Promise(resolve => {finish = resolve;}));
  await h.handlers['#btnScanSerial'](event);
  const pending = h.handlers['#btnBindSerial'](event);
  assert.equal(h.node('#btnConnect').disabled, true);
  assert.equal(h.node('#deviceId').disabled, true);
  assert.equal(h.node('#btnScanSerial').disabled, true);
  await h.handlers['#btnScanSerial'](event);
  assert.equal(h.calls.filter(([url]) => url.startsWith('/api/serial-ports')).length, 1);
  await h.handlers['#btnConnect'](event);
  assert.equal(h.calls.filter(([url]) => url === '/api/connect').length, 0);
  finish({});
  await pending;
  assert.equal(h.node('#serialPortSel').selectedOptions[0].dataset.bound, 'arm-01');
  assert.equal(h.node('#deviceId').disabled, false);
});

test('connecting blocks rebinding and a second connect request', async () => {
  let finish;
  const h = harness([], (url) => url.startsWith('/api/serial-ports')
    ? {ports: [{path: '/dev/cu.usbserial-new'}]}
    : url.startsWith('/api/status') ? h.ready : new Promise(resolve => {finish = resolve;}));
  await h.handlers['#btnScanSerial'](event);
  const pending = h.handlers['#btnConnect'](event);
  await h.handlers['#btnBindSerial'](event);
  await h.handlers['#btnConnect'](event);
  assert.equal(h.calls.filter(([url]) => url === '/api/connect').length, 1);
  assert.equal(h.calls.filter(([url]) => url === '/api/serial-bind').length, 0);
  finish({connected: true, session: 'new'});
  await pending;
});

test('late scan success or failure cannot overwrite a newly selected device', async () => {
  for (const failure of [false, true]) {
    let finish, fail;
    const h = harness([], () => new Promise((resolve, reject) => {finish = resolve; fail = reject;}));
    const pending = h.handlers['#btnScanSerial'](event);
    h.selectDevice('arm-02');
    h.node('#serialPortMsg').textContent = 'arm-02';
    if (failure) fail(new Error('old scan')); else finish({ports: [{path: '/dev/cu.old'}]});
    await pending;
    assert.equal(h.node('#serialPortMsg').textContent, 'arm-02');
    assert.equal(h.node('#serialPortSel').children.length, 0);
  }
});


test('only the most recent serial scan can update the selection', async () => {
  const resolve = [];
  const h = harness([], () => new Promise(done => resolve.push(done)));
  const first = h.handlers['#btnScanSerial'](event);
  const second = h.handlers['#btnScanSerial'](event);
  resolve[1]({ports: [{path: '/dev/cu.new', bound_device: 'arm-01'}]});
  await second;
  resolve[0]({ports: [{path: '/dev/cu.old', bound_device: 'arm-01'}]});
  await first;
  assert.equal(h.node('#serialPortSel').value, '/dev/cu.new');
});


test('connection waits for matching live position and blocks motion until completion', async () => {
  let connected, positioned;
  const h = harness([], url => new Promise(resolve => {
    if (url === '/api/connect') connected = resolve; else positioned = resolve;
  }));
  const pending = h.handlers['#btnConnect'](event);
  assert.match(h.node('#btnConnect').textContent, /等待就绪/);
  assert.equal(h.node('.jog').disabled, true);
  assert.equal(h.node('#btnDisconnect').disabled, true);
  assert.equal(h.node('#btnEstop').disabled, false);
  assert.equal(h.node('#btnStop').disabled, false);
  await h.context.motion('/api/jog', {axis: 'x', delta: 1}, 'moved', 'manual');
  assert.deepEqual(h.calls.map(([url]) => url), ['/api/connect']);
  h.context.renderStatus(h.ready); // Background polling cannot announce completion early.
  assert.match(h.node('#bDaemon').textContent, /等待就绪/);
  assert.equal(h.node('#btnDisconnect').disabled, true);
  connected({connected: true, session: 'new'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.context.S.busy, 1);
  assert.equal(h.notices.filter(n => n.kind === 'ok').length, 0);
  positioned(h.ready);
  await pending;
  assert.equal(h.context.S.busy, 0);
  assert.equal(h.node('.jog').disabled, false);
  assert.equal(h.node('#btnConnect').textContent, '🔌 连接设备');
  assert.match(h.notices.at(-1).message, /连接完成.*校准/);
  assert.equal(h.calls.filter(([url]) => url === '/api/jog').length, 0);
});

test('stale status is read again without repeating CONNECT', async () => {
  let reads = 0;
  const h = harness([], url => url === '/api/connect'
    ? {connected: true, session: 'new'}
    : ++reads === 1 ? {daemon: {status_stale: true}} : h.ready);
  await h.handlers['#btnConnect'](event);
  assert.equal(reads, 2);
  assert.equal(h.calls.filter(([url]) => url === '/api/connect').length, 1);
  assert.match(h.notices.at(-1).message, /连接完成/);
});

for (const failure of ['connect', 'disconnect', 'changed-session', 'stale', 'estop']) {
  test(`connection ${failure} does not announce success and releases pending controls`, async () => {
    const h = harness([], url => {
      if (url === '/api/connect') {
        if (failure === 'connect') throw new Error('连接超时');
        return {connected: true, session: 'new'};
      }
      if (failure === 'disconnect') return {daemon: {connected: false, disconnect_reason: '控制器复位'}};
      if (failure === 'changed-session') return {...h.ready, daemon: {...h.ready.daemon, session: 'other'}};
      if (failure === 'estop') return {...h.ready, daemon: {...h.ready.daemon, estop_locked: true}};
      return {daemon: {status_stale: true}};
    });
    await h.handlers['#btnConnect'](event);
    assert.equal(h.notices.filter(n => n.kind === 'ok').length, 0);
    assert.match(h.notices.at(-1).message, /连接未就绪/);
    assert.equal(h.context.S.busy, 0);
    assert.equal(h.node('#btnConnect').disabled, false);
    assert.equal(h.node('.jog').disabled, false);
    assert.equal(h.calls.filter(([url]) => url === '/api/connect').length, 1);
  });
}

test('late connect response cannot announce completion for another selected arm', async () => {
  let finish;
  const h = harness([], () => new Promise(resolve => {finish = resolve;}));
  const pending = h.handlers['#btnConnect'](event);
  h.selectDevice('arm-02');
  finish({connected: true, session: 'new'});
  await pending;
  assert.equal(h.notices.length, 0);
  assert.equal(h.context.S.busy, 0);
});
