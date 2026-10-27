const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../scripts/motor_debug_page.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const serial = script.slice(script.indexOf('// ---------- 串口换口检测'), script.indexOf('async function refreshCameraChannels'));

function harness(ports, respond) {
  const nodes = new Map(), handlers = {}, calls = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: '', children: [], style: {}, dataset: {},
      textContent: '', disabled: false,
      set innerHTML(v) { this.children = []; },
      get selectedOptions() {return this.children.filter(p => p.value === this.value);},
      appendChild(p) {this.children.push(p); if (p.selected || !this.value) this.value = p.value;},
      addEventListener(event, fn) {handlers[id] = fn;}});
    return nodes.get(id);
  };
  let device = 'arm-01';
  const context = {$: node, DEV_RAW: () => device, selectionGeneration: 1, S: {}, toast() {},
    document: {createElement: () => ({dataset: {}})},
    api: async (url, opts) => {calls.push([url, opts]); return respond ? respond(url, opts) : {ports};},
    refreshArmDevices: async () => {}, refreshStatus: async () => {}};
  vm.runInNewContext(serial, context);
  vm.runInNewContext(script.slice(script.indexOf('async function connectSelectedDevice()'),
    script.indexOf('$("#btnDisconnect").addEventListener')), context);
  return {node, handlers, calls, context, selectDevice(value) {device = value;}};
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
    : new Promise(resolve => {finish = resolve;}));
  await h.handlers['#btnScanSerial'](event);
  const pending = h.handlers['#btnConnect'](event);
  await h.handlers['#btnBindSerial'](event);
  await h.handlers['#btnConnect'](event);
  assert.equal(h.calls.filter(([url]) => url === '/api/connect').length, 1);
  assert.equal(h.calls.filter(([url]) => url === '/api/serial-bind').length, 0);
  finish({session: 'new'});
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
