// Pure UI tests: execute the page's actual gesture code with a fake DOM and fetch.
// No backend imports, sockets, calibration files, or real device requests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "../scripts/motor_debug_page.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const section = (start, end) => {
  const first = script.indexOf(start);
  assert.ok(first >= 0, `Missing page section start: ${start}`);
  const last = script.indexOf(end, first + start.length);
  assert.ok(last > first, `Missing page section end: ${end}`);
  return script.slice(first, last);
};
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const cameraPoint = (x, y) => ({cameraX: x, cameraY: y, frameW: 1280, frameH: 720});
const normalized = (x, y) => ({x, y});

function state(changes = {}) {
  return {deviceId: "arm-01", contextId: "ctx-1", requestId: "ticket-1", expiresInMs: 60000,
    binding: {calibrationFingerprint: "identity-hash", screenCalibrationFingerprint: "full-calibration-1",
      geometryHash: "geometry-1", zHash: "z-1",
      daemonSession: "session-1", frame: {width: 1280, height: 720}, screenOrientation: "portrait",
      cameraId: "cam-01", cameraUniqueId: "uvc-01", bindingRevision: 7},
    blocked: false, blockingReasons: [], lastResult: null, ...changes};
}
function plan(changes = {}) {
  const swipe = (id, label, start, end, durationMs) => ({
    id, label, action: "swipe", start, end, durationMs,
    points: {start: cameraPoint(start.x * 1280, start.y * 720),
      end: cameraPoint(end.x * 1280, end.y * 720)},
    calculated: true, blockingReasons: [], effectStatus: "unverified",
  });
  return {deviceId: "arm-01", contextId: "ctx-1", calibrationReady: true,
    calibrationFingerprint: "full-calibration-1", frameSeq: 100,
    binding: {device_id: "arm-01", daemon_session: "session-1", camera_id: "cam-01",
      camera_device_unique_id: "uvc-01", binding_revision: 7, frame_width: 1280, frame_height: 720,
      calibration_fingerprint: "full-calibration-1"},
    profile: {hoverZ: 2, tapDepth: 3, tapFeed: 1500},
    presets: [{id: "double_tap_center", label: "中心双击", action: "double_tap",
      point: normalized(.5, .5), intervalMs: 120, points: {point: cameraPoint(612, 350)},
      calculated: true, blockingReasons: [], effectStatus: "unverified"},
      swipe("swipe_up", "向上滑动", normalized(.5, .75), normalized(.5, .25), 740),
      swipe("swipe_down", "向下滑动", normalized(.5, .25), normalized(.5, .75), 741),
      swipe("swipe_left", "向左滑动", normalized(.75, .5), normalized(.25, .5), 742),
      swipe("swipe_right", "向右滑动", normalized(.25, .5), normalized(.75, .5), 743),
      swipe("bottom_up", "底部上滑", normalized(.5, .95), normalized(.5, .5), 900)],
    actions: {double_tap: {usable: true, blockingReasons: []},
      swipe: {usable: true, blockingReasons: []}}, blockingReasons: [], ...changes};
}
function acceptGesture(h, outcome = "confirmed") {
  h.responder = call => {
    if (call.url.pathname !== "/api/gestures/test") return undefined;
    const lastResult = {requestId: call.body.requestId, outcome, message: `模拟结果：${outcome}`,
      evidence: {motionConfirmed: outcome === "confirmed", controllerCompleted: outcome === "executed",
        ...(outcome === "executed" ? {observationNote: "旧探头照片无法匹配"} : {})}};
    const finished = ["confirmed", "executed"].includes(outcome);
    h.state = state({requestId: finished ? "ticket-2" : null,
      blocked: !finished, lastResult});
    return clone(lastResult);
  };
}
function previewGesture(h, call) {
  if (call.url.pathname !== "/api/gestures/preview") return undefined;
  const action = call.body.action, keys = action === "swipe" ? ["start", "end"] : ["point"];
  const item = {...clone(h.plan.presets[action === "swipe" ? 1 : 0]), id: "custom",
    deviceId: "arm-01", contextId: "ctx-1", binding: clone(h.plan.binding),
    calibrationFingerprint: h.plan.calibrationFingerprint, profile: clone(h.plan.profile), points: {}};
  for (const key of keys) {
    const p = call.body[key];
    item[key] = normalized(p.cameraX / p.frameW, p.cameraY / p.frameH);
    item.points[key] = clone(p);
  }
  return item;
}
function savedEntry(item, {id = "gesture-1", version = 1, label = "返回上一级", interval = 120} = {}) {
  const binding = clone(item.binding || plan().binding);
  const calculated = {...clone(item), id: "custom", label, binding};
  if (item.action === "double_tap") calculated.intervalMs = interval;
  return {contract_version: "saved-screen-gesture.v1", device_id: "arm-01", gesture_id: id,
    version, label, action: item.action, binding, usable: true, blocking_reasons: [],
    effect_status: "unverified", effect_status_display: "手机效果未验证",
    payload: {action: item.action, binding, ...(item.action === "swipe" ?
      {start: clone(item.start), end: clone(item.end), duration_ms: item.durationMs} :
      {point: clone(item.point), interval_ms: interval})}, plan: calculated};
}

function harness({defaultView = false} = {}) {
  const nodes = new Map(), radios = [];
  class Element {
    constructor(tag = "div", id = "") {
      this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.events = {};
      this.textContent = ""; this._value = ""; this._checked = false;
      this.disabled = false; this.hidden = false; this.open = false;
      this.style = {};
      this.classList = {toggle() {}};
      this.captured = new Set();
    }
    get options() { return this.children; }
    get value() { return this._value; }
    set value(value) { this._value = value; }
    get checked() { return this._checked; }
    set checked(value) {
      if (value && this.name) radios.filter(r => r.name === this.name).forEach(r => { r._checked = false; });
      this._checked = value;
    }
    replaceChildren() { this.children = []; if (this.tagName === "SELECT") this._value = ""; }
    append(child) {
      this.children.push(child);
      if (this.tagName === "SELECT" && this.children.length === 1) this._value = child.value;
    }
    addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
    async dispatch(name, event = {}) {
      for (const callback of this.events[name] || []) await callback({
        target: this, preventDefault() {}, stopPropagation() {}, ...event});
    }
    getBoundingClientRect() { return {left: 0, top: 0, width: 1280, height: 720}; }
    getContext(type) {
      assert.equal(this.tagName, "CANVAS");
      assert.equal(type, "2d");
      return {clearRect() {}, fillRect() {}, fillText() {}, strokeRect() {}, drawImage() {}};
    }
    setPointerCapture(id) { this.captured.add(id); }
    hasPointerCapture(id) { return this.captured.has(id); }
    releasePointerCapture(id) { this.captured.delete(id); }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  for (const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element = new Element(match[1], match[2]);
    if (element.tagName === "CANVAS") {
      element.width = Number(match[0].match(/\bwidth="(\d+)"/)?.[1] || 300);
      element.height = Number(match[0].match(/\bheight="(\d+)"/)?.[1] || 150);
    }
    nodes.set(match[2], element);
  }
  for (const [tag] of html.matchAll(/<input\b[^>]*type="radio"[^>]*>/g)) {
    const attr = name => tag.match(new RegExp(`\\b${name}="([^"]+)"`))?.[1];
    const element = nodes.get(attr("id")) || new Element("input");
    element.name = attr("name"); element.value = attr("value");
    radios.push(element);
    element.checked = /\schecked\b/.test(tag);
  }
  const query = selector => {
    if (selector.startsWith("#")) {
      assert.ok(nodes.has(selector.slice(1)), `Missing HTML element: ${selector}`);
      return nodes.get(selector.slice(1));
    }
    const name = selector.match(/name="([^"]+)"/)?.[1];
    const value = selector.match(/value="([^"]+)"/)?.[1];
    return radios.find(r => r.name === name && (!value || r.value === value) &&
      (!selector.includes(":checked") || r.checked));
  };
  const all = selector => selector.split(",").flatMap(s => s.startsWith("#") ? [query(s)] :
    radios.filter(r => r.name === s.match(/name="([^"]+)"/)?.[1]));
  // Legacy preset cases explicitly start in preset mode. Default-view tests use the real markup.
  nodes.get("basicMode").value = defaultView ?
    html.match(/<select id="basicMode">[\s\S]*?<option value="([^"]+)" selected>/)[1] : "preset";
  nodes.get("camimg").naturalWidth = 1280;
  nodes.get("camimg").naturalHeight = 720;
  nodes.get("basicInterval").value = html.match(/<input id="basicInterval"[^>]*value="([^"]+)"/)?.[1] || "";
  nodes.get("basicStrokeMm").value = html.match(/<input id="basicStrokeMm"[^>]*value="([^"]+)"/)?.[1] || "30";
  const storage = new Map(), calls = [], messages = [], timers = new Map();
  let timerId = 0;
  const h = {nodes, storage, calls, messages, timers, state: state(), plan: plan(), saved: [],
    responder: null, now: Date.now()};
  const context = vm.createContext({
    console, URL, URLSearchParams, Date: class extends Date { static now() { return h.now; } },
    setTimeout: (callback, delay) => {
      const id = ++timerId;
      timers.set(id, {callback, deadline: h.now + delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
    document: {querySelector: query, querySelectorAll: all,
      createElement: tag => new Element(tag)},
    window: Object.assign(new Element("window"), {location: {origin: "http://ui-test.invalid"}}),
    location: {origin: "http://ui-test.invalid"},
    sessionStorage: {getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    toast: (message, type) => messages.push({message, type}),
    confirm: () => { assert.fail("Single gesture must not open a browser confirmation"); },
    redrawCamera() {}, refreshStatus() {}, renderWizard() {},
    setBadge: (element, state, text) => { element.textContent = text; },
    motion: () => { assert.fail("Drawing must never call the legacy motion path"); },
    recoverDeviceContext: () => { throw new Error("Unexpected context recovery"); },
    fetch: async (url, options) => {
      const call = {url: new URL(url, "http://ui-test.invalid"), method: options.method || "GET", cache: options.cache,
        headers: {...options.headers}, body: options.body ? JSON.parse(options.body) : null};
      assert.equal(call.url.origin, "http://ui-test.invalid", "fixture requests stay isolated");
      if (["POST", "PUT", "DELETE"].includes(call.method)) {
        assert.ok(h.run("DEBUG_CSRF"), "a debug session must be established before writes");
        assert.equal(call.headers["X-Debug-CSRF"], h.run("DEBUG_CSRF"));
      }
      calls.push(call);
      let data;
      if (h.responder) data = await h.responder(call);
      if (data === undefined) {
        assert.equal(call.method, "GET", `Unexpected write: ${call.url.pathname}`);
        if (call.url.pathname === "/api/gestures/state") data = clone(h.state);
        else if (call.url.pathname === "/api/gestures/plan") data = clone(h.plan);
        else if (call.url.pathname === "/api/gestures/saved")
          data = {deviceId: "arm-01", contextId: "ctx-1", gestures: clone(h.saved)};
        else throw new Error(`Unexpected endpoint: ${call.url.pathname}`);
      }
      return {ok: !data.httpError, status: data.httpError || 200,
        json: async () => data.httpError ? {error: {message: data.message || `HTTP ${data.httpError}`}} : data};
    },
  });
  vm.runInContext(`
    const $ = s => document.querySelector(s), $$ = s => document.querySelectorAll(s);
    let selectedDevice = "arm-01", deviceSelectionError = "", WZ = null;
    const DEV_RAW = () => selectedDevice, DEV = () => encodeURIComponent(selectedDevice);
    ${section("const S = {", "const ENTRY_PARAMS")}
    S.contextId = "ctx-1";
    S.context = {daemonSession: "session-1", cameraId: "cam-01", cameraDeviceUniqueId: "uvc-01",
      bindingRevision: 7, frameWidth: 1280, frameHeight: 720};
    S.camSeq = 199;
    function setBusy(delta) { S.busy = Math.max(0, S.busy + delta); renderBasic(); }
    ${section("let DEBUG_CSRF =", "async function api(")}
    // Session bootstrap is already complete for gesture cases; it adds no gesture request.
    DEBUG_CSRF = "isolated-debug-csrf";
    ${section("let contextRecovery =", "async function recoverDeviceContext()")}
    ${section("async function api(", "function setBusy(")}
    ${section("// ---- FIX-12 手机粗框（真实源预览）BEGIN ----",
      "// ---- FIX-12 手机粗框（真实源预览）END ----")}
    regionInvalidate(DEV_RAW(), S.contextId);
    ${section('$("#camwrap").addEventListener("click"', "function basicAction()")}
    ${section("function basicAction()", "function renderGesture()")}
    ${section("async function refreshWizard()", '$("#btnWzStart")')}
  `, context);
  h.run = expression => vm.runInContext(expression, context);
  h.put = (name, value) => { context[name] = clone(value); };
  h.value = expression => clone(h.run(expression));
  h.el = id => nodes.get(id);
  h.click = id => h.el(id).dispatch("click");
  h.pointer = (type, x, y, changes = {}) => h.el("camwrap").dispatch(type, {
    clientX: x, clientY: y, pointerId: 1, button: 0, isPrimary: true, ...changes});
  h.draw = async (start, end = start) => {
    await h.pointer("pointerdown", ...start);
    await h.pointer("pointermove", ...end);
    await h.pointer("pointerup", ...end);
    await h.el("camwrap").dispatch("click", {clientX: end[0], clientY: end[1]});
    await h.flush();
  };
  h.change = async (id, value) => { h.el(id).value = value; await h.el(id).dispatch("change"); };
  h.direction = async value => {
    const radio = radios.find(r => r.name === "basicDirection" && r.value === value);
    assert.ok(radio, `Missing direction: ${value}`);
    radio.checked = true; await radio.dispatch("change");
  };
  h.manual = async action => {
    await h.change("basicMode", "manual");
    const radio = radios.find(r => r.name === "basicAction" && r.value === action);
    radio.checked = true; await radio.dispatch("change");
  };
  h.mark = (x, y, changes = {}) => {
    h.put("markChanges", changes);
    h.run(`S.lastMark = {nx: ${x}, ny: ${y}, deviceId: DEV_RAW(), contextId: S.contextId,
      frameW: 1280, frameH: 720, frameSeq: S.camSeq, basicRequestId: BASIC.state.requestId,
      basicPlanLoad: BASIC.planLoad, ...markChanges}`);
  };
  h.flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
  h.advance = ms => {
    h.now += ms;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.deadline <= h.now)
        .sort((a, b) => a[1].deadline - b[1].deadline)[0];
      if (!next) break;
      timers.delete(next[0]); next[1].callback();
    }
  };
  h.load = () => h.run("loadBasicState()");
  return h;
}

test("page script parses; only the double-tap interval and swipe stroke length are editable, not calibrated mechanical fields", () => {
  new vm.Script(script);
  const panel = html.slice(html.indexOf('id="basicGesturePanel"'), html.indexOf('id="ocrNx"'));
  assert.match(panel, /<input id="basicInterval" type="number" min="50" max="500" step="10" value="50" required>/);
  assert.match(panel, /抬起后停顿\(ms\)/);
  assert.match(panel, /<output id="basicDuration">/);
  assert.match(panel, /滑动长度\(mm\)/);
  // 数字输入恰两个：抬起停顿 + 滑动长度（手势形状参数，用户拍板可调）；
  // 校准机械参数（tapDepth/tapFeed/hoverZ/feed/durationMs）仍禁止暴露。
  assert.equal([...panel.matchAll(/<input[^>]+type="number"/g)].length, 2);
  assert.doesNotMatch(panel, /<input[^>]+(?:id|name)="(?:tapDepth|tapFeed|hoverZ|feed|durationMs)"/);
  assert.match(panel, /id="basicDoubleTap"[^>]*>双击<\/button>/);
  assert.match(panel, /id="basicTest"[^>]*disabled>执行双击<\/button>/);
  assert.equal([...panel.matchAll(/name="basicDirection"/g)].length, 4);
  assert.doesNotMatch(panel, /name="basicDirection"[^>]*checked/);
  assert.doesNotMatch(html, /id="basicConfirm|id="basicPreset"/);
  assert.doesNotMatch(section("function basicAction()", "function renderGesture()"), /\bconfirm\(/);
  assert.match(script, /await syncBasicCalibration\(wizard\)/);
});

test("initial plan keeps direction unselected and center double tap uses API auto-context", async () => {
  const h = harness();
  await h.load();
  assert.equal(h.run("basicDirection()"), "");
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicDoubleTap").disabled, false);
  assert.match(h.el("basicCalculationSummary").textContent, /滑动方向未选择/);
  assert.doesNotMatch(h.el("basicCalculationSummary").textContent, /禁止执行/);
  assert.deepEqual(h.calls.map(c => [c.method, c.url.pathname]), [
    ["GET", "/api/gestures/state"], ["GET", "/api/gestures/plan"],
  ]);
  for (const call of h.calls) {
    assert.equal(call.url.searchParams.get("deviceId"), "arm-01");
    assert.equal(call.url.searchParams.get("contextId"), "ctx-1");
  }
  const payload = h.value('basicPayload("double_tap_center")');
  assert.deepEqual(payload.binding, state().binding);
  assert.notDeepEqual(payload.binding, plan().binding);
  assert.deepEqual(payload.point, normalized(.5, .5));
  assert.equal(payload.coordinateSpace, "screen_normalized");
  assert.equal(payload.frameSeq, 100);
  assert.equal(payload.intervalMs, 50);
  assert.deepEqual(h.value("BASIC.marks.start"), cameraPoint(612, 350));
  assert.doesNotMatch(JSON.stringify(payload), /cameraX|tapDepth|hoverZ|tapFeed/);
});

test("all six presets use calculated points and times; bottom-up is explicitly unverified", async () => {
  const h = harness(); await h.load();
  for (const preset of h.plan.presets) {
    if (preset.id.startsWith("swipe_")) await h.direction(preset.id.slice(6));
    else if (preset.id === "bottom_up") await h.change("basicMode", "bottom_up");
    const payload = h.value(preset.action === "double_tap" ? 'basicPayload("double_tap_center")' : "basicPayload()");
    assert.equal(payload.action, preset.action);
    assert.deepEqual(payload.point || payload.start, preset.point || preset.start);
    assert.equal(payload.durationMs || payload.intervalMs,
      preset.action === "double_tap" ? Number(h.el("basicInterval").value) : preset.durationMs);
  }
  assert.match(h.el("basicCalculationSummary").textContent, /候选.*未验证/);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("no direction never sends a request, even when a disabled click is dispatched", async () => {
  const h = harness(); await h.load();
  const count = h.calls.length;
  await h.click("basicTest");
  assert.equal(h.calls.length, count);
  assert.throws(() => h.run("basicPayload()"), /请选择滑动方向/);
  assert.match(h.messages.at(-1).message, /请选择滑动方向/);
  await h.click("basicRecalculate");
  assert.equal(h.run("basicDirection()"), "");
  assert.equal(h.el("basicTest").disabled, true);
});

for (const direction of ["up", "down", "left", "right"])
  test(`select ${direction} locally; one click sends exactly its calculated swipe`, async () => {
    const h = harness(); await h.load(); acceptGesture(h);
    const count = h.calls.length;
    await h.direction(direction);
    assert.equal(h.calls.length, count);
    assert.equal(h.el("basicTest").disabled, false);
    assert.equal(h.run('$$(\'input[name="basicDirection"]\').filter(input => input.checked).length'), 1);
    const preset = h.plan.presets.find(item => item.id === `swipe_${direction}`);
    await h.click("basicTest");
    const writes = h.calls.filter(c => c.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url.pathname, "/api/gestures/test");
    assert.equal(writes[0].body.action, "swipe");
    assert.deepEqual(writes[0].body.start, preset.start);
    assert.deepEqual(writes[0].body.end, preset.end);
    assert.equal(writes[0].body.durationMs, preset.durationMs);
    assert.equal(writes[0].body.strokeMm, 30); // 默认=已验证 30mm 行程
    assert.deepEqual(writes[0].body.binding, state().binding);
    assert.equal(writes[0].body.requestId, "ticket-1");
    assert.equal(writes[0].body.operator, "调试台");
    assert.equal(writes[0].body.confirmed, true);
  });

test("stroke length input defaults to verified 30mm and sends user-adjusted value", async () => {
  const h = harness(); await h.load(); acceptGesture(h);
  assert.equal(h.el("basicStrokeMm").value, "30");
  await h.direction("up");
  h.el("basicStrokeMm").value = "45";
  await h.click("basicTest");
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.strokeMm, 45);
});

test("changing direction replaces the preview without sending or leaving multiple choices", async () => {
  const h = harness(); await h.load();
  const count = h.calls.length;
  await h.direction("up"); await h.direction("right");
  assert.equal(h.el("basicDirectionUp").checked, false);
  assert.equal(h.run("basicDirection()"), "right");
  assert.deepEqual(h.value("basicPayload().end"), normalized(.75, .5));
  assert.equal(h.calls.length, count);
});

test("controller-completed debug gesture shows observation separately and needs no manual release", async () => {
  const h = harness(); await h.load(); acceptGesture(h, "executed");
  await h.direction("up");
  await h.click("basicTest");
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.el("basicReview").hidden, true);
  assert.equal(h.el("basicTest").disabled, false);
  assert.match(h.el("basicStatus").textContent, /指令执行：已完成/);
  assert.match(h.el("basicStatus").textContent, /机械动作：未确认/);
  assert.match(h.el("basicStatus").textContent, /旧探头照片无法匹配/);
  assert.doesNotMatch(h.el("basicStatus").textContent, /禁止重发|停止阶段/);
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 1);
});

test("center double-tap remains an independent one-click command after selecting a swipe", async () => {
  const h = harness(); await h.load(); await h.direction("left"); acceptGesture(h);
  await h.click("basicDoubleTap");
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.action, "double_tap");
  assert.deepEqual(writes[0].body.point, normalized(.5, .5));
  assert.equal(h.run("basicDirection()"), "left");
});

test("missing selected direction never falls back to center or another swipe", async () => {
  const h = harness(); h.plan.presets = h.plan.presets.filter(p => p.id !== "swipe_up");
  await h.load(); await h.direction("up");
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicDoubleTap").disabled, false);
  await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  assert.equal(h.run("BASIC.calculation"), null);
});

test("missing center never turns the double-tap button into a swipe", async () => {
  const h = harness(); h.plan.presets.shift(); await h.load();
  assert.equal(h.el("basicDoubleTap").disabled, true);
  await h.click("basicDoubleTap");
  await h.direction("up");
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

for (const mutation of [
  p => { p.presets[1].blockingReasons = ["unsafe swipe"]; },
  p => { p.presets[1].action = "double_tap"; },
  p => { p.actions.swipe.usable = false; },
]) test(`selected swipe still respects item and capability gates: ${mutation}`, async () => {
  const h = harness(); mutation(h.plan); await h.load(); await h.direction("up");
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicDoubleTap").disabled, false);
  await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("operator is absent and standard debug identity never blocks valid actions", async () => {
  const h = harness(); await h.load();
  assert.doesNotMatch(html, /id="basicOperator"/);
  assert.equal(h.el("basicDoubleTap").disabled, false);
  assert.equal(h.run('basicPayload("double_tap_center").operator'), "调试台");
  assert.match(h.el("basicTestReason").textContent, /请选择滑动方向/);
  await h.direction("down"); assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.run("basicPayload().operator"), "调试台");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("changing point mode or device clears direction and leaves no duplicate visible commands", async () => {
  const h = harness(); await h.load(); await h.direction("up");
  await h.manual("double_tap");
  assert.equal(h.el("basicDoubleTap").hidden, true);
  assert.equal(h.el("basicDirections").hidden, true);
  assert.equal(h.el("basicTest").textContent, "执行双击");
  await h.change("basicMode", "preset");
  assert.equal(h.run("basicDirection()"), "");
  assert.equal(h.el("basicDoubleTap").hidden, false);
  assert.equal(h.el("basicTest").disabled, true);
  await h.direction("right");
  h.run('selectionGeneration++; selectedDevice = "arm-02"; S.contextId = "ctx-2"; invalidateBasicMarks()');
  assert.equal(h.run("basicDirection()"), "");
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.equal(h.el("basicTest").disabled, true);
});

test("not-ready discards even unsolicited presets and does not manufacture defaults", async () => {
  const h = harness(); h.plan.calibrationReady = false;
  h.plan.binding = null; h.plan.calibrationFingerprint = null; h.plan.frameSeq = null;
  await h.load();
  assert.deepEqual(h.value("BASIC.marks"), {});
  assert.equal(h.run("BASIC.calculation"), null);
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.match(h.el("basicPlanSummary").textContent, /尚未就绪/);
});

for (const [label, mutate] of [
  ["not calculated", p => { p.presets[0].calculated = false; }],
  ["blocked preset", p => { p.presets[0].blockingReasons = ["unsafe point"]; }],
  ["missing feature", p => { p.actions.double_tap = {usable: false, blockingReasons: ["feature missing"]}; }],
  ["global gate", p => { p.blockingReasons = ["foreign lease"]; }],
  ["fingerprint mismatch", p => { p.calibrationFingerprint = "other-version"; }],
  ["basic/full fingerprint mismatch", p => {
    p.calibrationFingerprint = p.binding.calibration_fingerprint = "other-version";
  }],
  ["preset fingerprint mismatch", p => { p.presets[0].calibrationFingerprint = "other-version"; }],
  ["preset binding mismatch", p => { p.presets[0].binding = {...p.binding, binding_revision: 9}; }],
  ["session mismatch", p => { p.binding.daemon_session = "session-2"; }],
  ["camera mismatch", p => { p.binding.camera_device_unique_id = "uvc-02"; }],
  ["frame mismatch", p => { p.binding.frame_width = 640; }],
  ["missing frame", p => { p.frameSeq = null; }],
  ["invalid point", p => { p.presets[0].point.x = 1.1; }],
  ["camera-only point", p => { p.presets[0].point = cameraPoint(100, 200); }],
  ["invalid camera overlay", p => { p.presets[0].points.point.frameW = 640; }],
  ["invalid timing", p => { p.presets[0].intervalMs = "120"; }],
  ["unverified contract", p => { p.presets[0].effectStatus = "confirmed"; }],
]) test(`fail closed: ${label}`, async () => {
  const h = harness(); mutate(h.plan); await h.load();
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.throws(() => h.run('basicPayload("double_tap_center")'));
  await h.click("basicDoubleTap");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("plan 404 explicitly reports unloaded and never falls back to a camera action", async () => {
  const h = harness();
  h.responder = c => c.url.pathname === "/api/gestures/plan" ? {httpError: 404} : undefined;
  await h.load(); await h.click("basicDoubleTap");
  assert.match(h.el("basicPlanSummary").textContent, /未加载.*404/);
  assert.equal(h.el("basicTest").disabled, true);
  assert.deepEqual(h.calls.map(c => c.url.pathname), ["/api/gestures/state", "/api/gestures/plan"]);
});

test("old state without a full screen-calibration fingerprint cannot authorize execution", async () => {
  const h = harness(); delete h.state.binding.screenCalibrationFingerprint;
  await h.load();
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.throws(() => h.run('basicPayload("double_tap_center")'), /版本/);
});

test("manual points require preview, use server inverse result, and keep the plan's frame", async () => {
  const h = harness(); await h.load(); await h.manual("double_tap");
  assert.throws(() => h.run("basicPayload()"), /尚未计算/);
  const pending = deferred();
  h.responder = c => c.url.pathname === "/api/gestures/preview" ? pending.promise : undefined;
  h.mark(.2, .3); await h.click("basicStart");
  assert.equal(h.el("basicTest").disabled, true);
  const call = h.calls.at(-1);
  assert.equal(call.method, "POST");
  assert.deepEqual(call.body, {deviceId: "arm-01", contextId: "ctx-1", action: "double_tap",
    point: cameraPoint(256, 216)});
  pending.resolve({...h.plan.presets[0], id: "manual", point: normalized(.31, .42),
    points: {point: cameraPoint(256, 216)}});
  await h.flush();
  const payload = h.value("basicPayload()");
  assert.deepEqual(payload.point, normalized(.31, .42));
  assert.equal(payload.frameSeq, 100);
  assert.equal(h.el("basicInterval").value, "50");
  assert.equal(h.calls.some(c => c.url.pathname === "/api/gestures/test"), false);
});

test("manual swipe waits for both marks and swapping requires a fresh preview", async () => {
  const h = harness(); await h.load(); await h.manual("swipe");
  h.responder = c => c.url.pathname === "/api/gestures/preview" ? {
    ...h.plan.presets[1], id: "manual", durationMs: c.body.start.cameraY > c.body.end.cameraY ? 876 : 934,
    start: normalized(c.body.start.cameraX / 1280, c.body.start.cameraY / 720),
    end: normalized(c.body.end.cameraX / 1280, c.body.end.cameraY / 720),
    points: {start: c.body.start, end: c.body.end},
  } : undefined;
  h.mark(.5, .75); await h.click("basicStart");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  h.mark(.5, .25); await h.click("basicEnd"); await h.flush();
  assert.equal(h.run("basicPayload().durationMs"), 876);
  assert.equal(h.el("basicTest").disabled, false);
  await h.click("basicSwap"); await h.flush();
  assert.equal(h.run("basicPayload().durationMs"), 934);
  assert.deepEqual(h.value("basicPayload().start"), normalized(.5, .25));
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/preview").length, 2);
});

test("preview 404 cannot enable a legacy execution path", async () => {
  const h = harness(); await h.load(); await h.manual("double_tap");
  h.responder = c => c.url.pathname === "/api/gestures/preview" ? {httpError: 404} : undefined;
  h.mark(.2, .3); await h.click("basicStart"); await h.flush();
  assert.match(h.el("basicCalculationSummary").textContent, /未加载.*404/);
  assert.throws(() => h.run("basicPayload()"), /未加载/);
  await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
});

test("unavailable action features still permit read-only arbitrary-point calculation", async () => {
  const h = harness();
  h.plan.blockingReasons = ["motion verifier unavailable"];
  h.plan.actions.double_tap = {usable: false, blockingReasons: ["feature missing"]};
  await h.load(); await h.manual("double_tap");
  h.responder = c => c.url.pathname === "/api/gestures/preview" ? {
    ...h.plan.presets[0], binding: h.plan.binding,
    calibrationFingerprint: h.plan.calibrationFingerprint,
    deviceId: "arm-01", contextId: "ctx-1",
  } : undefined;
  assert.equal(h.el("basicStart").disabled, false);
  h.mark(.2, .3); await h.click("basicStart"); await h.flush();
  assert.equal(h.run("BASIC.calculation.item.calculated"), true);
  assert.match(h.el("basicCalculationSummary").textContent, /屏幕点：.*\n禁止执行：/);
  assert.equal(h.el("basicTest").disabled, true);
  assert.throws(() => h.run("basicPayload()"), /motion verifier unavailable/);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
  assert.equal(h.calls.at(-1).url.pathname, "/api/gestures/preview");
});

for (const changed of [
  {calibrationFingerprint: "other-version"}, {binding: {...plan().binding, binding_revision: 9}},
  {deviceId: "arm-02"}, {contextId: "ctx-2"},
]) test(`preview version/identity mismatch rejects: ${JSON.stringify(changed)}`, async () => {
  const h = harness(); await h.load(); await h.manual("double_tap");
  h.responder = c => c.url.pathname === "/api/gestures/preview" ? {...h.plan.presets[0], ...changed} : undefined;
  h.mark(.2, .3); await h.click("basicStart"); await h.flush();
  assert.equal(h.run("BASIC.calculation"), null);
  assert.throws(() => h.run("basicPayload()"), /版本不符/);
});

test("requestId refresh invalidates old marks and in-flight manual previews", async () => {
  const h = harness(); await h.load(); await h.manual("double_tap");
  const pending = deferred();
  h.responder = c => c.url.pathname === "/api/gestures/preview" ? pending.promise : undefined;
  h.mark(.2, .3); await h.click("basicStart");
  h.state.requestId = "ticket-2"; await h.load();
  pending.resolve(h.plan.presets[0]); await h.flush();
  assert.deepEqual(h.value("BASIC.marks"), {});
  assert.equal(h.run("S.lastMark"), null);
  assert.equal(h.run("BASIC.calculation"), null);
  h.mark(.2, .3, {basicRequestId: "ticket-1"});
  await h.click("basicStart");
  assert.deepEqual(h.value("BASIC.marks"), {});
  assert.equal(h.el("basicTest").disabled, true);
});

test("out-of-order plan and state responses cannot replace newer snapshots", async () => {
  const h = harness(); await h.load();
  const pendingPlan = deferred();
  let count = 0;
  h.responder = c => c.url.pathname === "/api/gestures/plan" && ++count === 1 ? pendingPlan.promise : undefined;
  const oldPlan = h.run("loadBasicPlan()");
  h.plan.frameSeq = 110; await h.run("loadBasicPlan()");
  pendingPlan.resolve(plan({frameSeq: 90})); await oldPlan;
  assert.equal(h.run("BASIC.plan.frameSeq"), 110);
  const pendingState = deferred(); count = 0;
  h.responder = c => c.url.pathname === "/api/gestures/state" && ++count === 1 ? pendingState.promise : undefined;
  const oldState = h.load();
  h.state.requestId = "ticket-new"; await h.load();
  pendingState.resolve(state()); await oldState;
  assert.equal(h.run("BASIC.state.requestId"), "ticket-new");
});

for (const endpoint of ["plan", "state", "preview"]) test(`device/context switch rejects late ${endpoint}`, async () => {
  const h = harness(); await h.load();
  if (endpoint === "preview") {
    await h.manual("double_tap"); h.put("marks", {start: cameraPoint(200, 250)});
    h.run("BASIC.marks = marks");
  }
  const pending = deferred();
  h.responder = c => c.url.pathname === `/api/gestures/${endpoint}` ? pending.promise : undefined;
  const operation = h.run(endpoint === "preview" ? "previewBasicMarks()" :
    endpoint === "plan" ? "loadBasicPlan()" : "loadBasicState()");
  h.run('selectionGeneration++; selectedDevice = "arm-02"; S.contextId = "ctx-2"; invalidateBasicMarks()');
  pending.resolve(endpoint === "preview" ? plan().presets[0] : endpoint === "plan" ? plan() : state());
  await operation;
  assert.equal(h.run("BASIC.state"), null);
  assert.equal(h.run("BASIC.plan"), null);
  assert.equal(h.run("BASIC.calculation"), null);
});

test("ready transition and full calibration changes reload without polling churn", async () => {
  const h = harness(); await h.load();
  h.put("wizard", {bundle: {status: "draft", xy: {matrix: [1]}, z: {tapDepth: 3}}});
  h.plan = plan({calibrationReady: false, presets: []});
  await h.run("syncBasicCalibration(wizard)");
  assert.equal(h.el("basicTest").disabled, true);
  h.plan = plan(); h.run('wizard.bundle.status = "ready"');
  await h.run("syncBasicCalibration(wizard)");
  assert.equal(h.el("basicDoubleTap").disabled, false);
  const count = h.calls.length;
  h.run('wizard.bundle.updatedAt = "new timestamp"');
  await h.run("syncBasicCalibration(wizard)");
  assert.equal(h.calls.length, count);
  h.run("wizard.bundle.z.tapDepth = 4");
  h.plan.calibrationFingerprint = h.plan.binding.calibration_fingerprint = "full-calibration-2";
  h.state.binding.screenCalibrationFingerprint = "full-calibration-2";
  h.state.binding.zHash = "z-2"; h.state.requestId = "ticket-2";
  await h.run("syncBasicCalibration(wizard)");
  assert.equal(h.run("BASIC.plan.calibrationFingerprint"), "full-calibration-2");
  assert.ok(h.calls.length > count);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("late wizard response cannot change calibration on a newly selected device", async () => {
  const h = harness(); await h.load();
  const pending = deferred();
  h.responder = c => c.url.pathname === "/api/wizard/state" ? pending.promise : undefined;
  const operation = h.run("refreshWizard()");
  h.run('selectionGeneration++; selectedDevice = "arm-02"; S.contextId = "ctx-2"; invalidateBasicMarks()');
  pending.resolve({bundle: {status: "ready"}}); await operation;
  assert.equal(h.run("WZ"), null);
  assert.equal(h.run("BASIC.bundleKey"), null);
});

test("older wizard poll cannot overwrite a newer full-calibration snapshot", async () => {
  const h = harness(); await h.load();
  const pending = deferred();
  let count = 0;
  h.responder = c => c.url.pathname === "/api/wizard/state" ?
    (++count === 1 ? pending.promise : {bundle: {status: "ready", z: {tapDepth: 4}}}) : undefined;
  const old = h.run("refreshWizard()");
  await h.run("refreshWizard()");
  pending.resolve({bundle: {status: "draft", z: {tapDepth: 3}}}); await old;
  assert.equal(h.run("WZ.bundle.status"), "ready");
  assert.equal(h.run("WZ.bundle.z.tapDepth"), 4);
});

test("recalculation preserves unknown result, evidence, storage and disabled execution", async () => {
  const h = harness(); await h.load();
  h.run('BASIC.uncertain = {requestId: "unknown-1", session: "session-1"}');
  h.storage.set("basic-gesture-pending:arm-01", JSON.stringify(h.value("BASIC.uncertain")));
  h.el("basicStatus").textContent = "Original unknown result";
  const count = h.calls.length;
  await h.click("basicRecalculate");
  assert.equal(h.el("basicStatus").textContent, "Original unknown result");
  assert.equal(h.run("BASIC.uncertain.requestId"), "unknown-1");
  assert.ok(h.storage.has("basic-gesture-pending:arm-01"));
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicReview").hidden, false);
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]), [["GET", "/api/gestures/plan"]]);
  h.state.lastResult = {requestId: "unknown-1", outcome: "confirmed"};
  await h.run("loadBasicState(false)");
  assert.equal(h.run("BASIC.uncertain.requestId"), "unknown-1");
  assert.match(h.el("basicStatus").textContent, /^Original unknown result/);
  assert.equal(h.calls.at(-1).url.searchParams.has("requestId"), false);
});

test("execution uses debug identity and rejects changed direction or calculation binding", async () => {
  const h = harness(); await h.load();
  await h.direction("up");
  assert.equal(h.run("basicPayload().operator"), "调试台");
  h.el("basicDirectionDown").checked = true;
  await h.click("basicTest");
  assert.match(h.messages.at(-1).message, /所选手势不符/);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  await h.direction("down");
  h.run('BASIC.plan.calibrationFingerprint = "changed"');
  await h.click("basicTest"); await h.click("basicDoubleTap");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("expiry alone leaves valid gesture buttons enabled and preserves the previous result", async () => {
  const h = harness();
  h.state.lastResult = {requestId: "previous-ticket", outcome: "confirmed", message: "previous result",
    evidence: {motionConfirmed: true, before: true}};
  await h.load(); await h.direction("up");
  const result = h.value("BASIC.state.lastResult"), status = h.el("basicStatus").textContent;
  const evidence = [...h.el("basicEvidence").children], calls = h.calls.length;
  const calculation = h.value("BASIC.calculation"), summary = h.el("basicCalculationSummary").textContent;
  h.run("BASIC.expires = Date.now(); renderBasic(); renderBasic()");
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.el("basicDoubleTap").disabled, false);
  assert.equal(h.el("basicTestReason").textContent, "");
  assert.equal(h.el("basicCalculationSummary").textContent, summary);
  assert.deepEqual(h.value("BASIC.calculation"), calculation);
  assert.equal(h.el("basicStatus").textContent, status);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.deepEqual(h.el("basicEvidence").children, evidence);
  assert.equal(h.calls.length, calls);
  h.state.requestId = "renewed-ticket";
  await h.load();
  assert.equal(h.el("basicDoubleTap").disabled, false);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.equal(h.el("basicStatus").textContent, status);
});

test("expiry never clears unknown outcome, uncertainty, storage or review controls", async () => {
  const h = harness();
  h.state.lastResult = {requestId: "unknown-1", outcome: "unknown", message: "original unknown",
    evidence: {motionConfirmed: false}};
  h.storage.set("basic-gesture-pending:arm-01",
    JSON.stringify({requestId: "unknown-1", session: "session-1"}));
  await h.load();
  const result = h.value("BASIC.state.lastResult"), status = h.el("basicStatus").textContent;
  const uncertain = h.value("BASIC.uncertain"), stored = [...h.storage], calls = h.calls.length;
  h.run("BASIC.expires = Date.now(); renderBasic()");
  assert.match(h.el("basicTestReason").textContent, /原请求结果未核实/);
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicReview").hidden, false);
  assert.equal(h.el("basicStatus").textContent, status);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.deepEqual(h.value("BASIC.uncertain"), uncertain);
  assert.deepEqual([...h.storage], stored);
  assert.equal(h.calls.length, calls);
  await h.click("basicDoubleTap"); await h.click("basicTest");
  assert.equal(h.calls.length, calls);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.deepEqual(h.value("BASIC.uncertain"), uncertain);
  assert.deepEqual([...h.storage], stored);
});

for (const change of ["BASIC.state.requestId = null", 'S.contextId = "other-context"',
  'selectedDevice = "arm-02"']) test(`expiry cannot bypass missing or foreign context: ${change}`, async () => {
  const h = harness(); await h.load();
  h.run(`BASIC.expires = 0; ${change}; renderBasic()`);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  const count = h.calls.length;
  await h.click("basicDoubleTap");
  assert.equal(h.calls.length, count);
});

for (const guard of ["BASIC.busy = true", "S.busy = 1",
  "BASIC.state.blocked = true",
  'BASIC.state.requestId = "changed"', 'S.contextId = "changed"'])
  test(`existing execution guard remains effective: ${guard}`, async () => {
    const h = harness(); await h.load(); await h.direction("up");
    h.run(guard); await h.click("basicTest"); await h.click("basicDoubleTap");
    assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  });

test("one double-tap click sends one normalized request and unknown is never replayed", async () => {
  const h = harness(); await h.load();
  h.responder = c => {
    if (c.url.pathname !== "/api/gestures/test") return undefined;
    h.state = state({requestId: null, blocked: true, blockingReasons: ["result unknown"],
      lastResult: {requestId: "ticket-1", outcome: "unknown", message: "result unknown",
        evidence: {motionConfirmed: false}}});
    throw new Error("simulated lost response");
  };
  await h.click("basicDoubleTap");
  await h.click("basicDoubleTap"); await h.direction("up"); await h.click("basicTest");
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url.pathname, "/api/gestures/test");
  assert.deepEqual(writes[0].body, {deviceId: "arm-01", contextId: "ctx-1",
    action: "double_tap", operator: "调试台", confirmed: true, requestId: "ticket-1",
    binding: state().binding, coordinateSpace: "screen_normalized", frameSeq: 100,
    point: normalized(.5, .5), intervalMs: 50});
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.equal(h.run("BASIC.uncertain.requestId"), "ticket-1");
  assert.match(h.el("basicStatus").textContent, /result unknown/);
});

for (const button of ["basicDoubleTap", "basicTest"])
  test(`rapid clicks and delayed reconciliation send one request only: ${button}`, async () => {
    const h = harness(); await h.load(); await h.direction("up");
    const request = deferred(), reconciliation = deferred();
    h.responder = c => c.url.pathname === "/api/gestures/test" ? request.promise :
      c.url.pathname === "/api/gestures/state" ? reconciliation.promise : undefined;
    const first = h.click(button);
    await h.click(button); await h.click("basicDoubleTap"); await h.click("basicTest");
    assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
    assert.equal(h.el("basicDoubleTap").disabled, true);
    assert.equal(h.el("basicTest").disabled, true);
    assert.ok(h.storage.has("basic-gesture-pending:arm-01"));
    request.resolve({requestId: "ticket-1", outcome: "confirmed",
      evidence: {motionConfirmed: true}});
    await h.flush();
    await h.click(button);
    assert.equal(h.run("BASIC.busy"), true);
    assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
    reconciliation.resolve(state({requestId: "ticket-2",
      lastResult: {requestId: "ticket-1", outcome: "confirmed"}}));
    await first;
    assert.equal(h.run("BASIC.busy"), false);
    assert.equal(h.run("BASIC.uncertain"), null);
    assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
  });

test("a consumed ticket cannot be reused even if a state response mistakenly repeats it", async () => {
  const h = harness(); await h.load();
  h.responder = c => {
    if (c.url.pathname !== "/api/gestures/test") return undefined;
    h.state.lastResult = {requestId: "ticket-1", outcome: "confirmed"};
    return clone(h.state.lastResult);
  };
  await h.click("basicDoubleTap"); await h.click("basicDoubleTap");
  await h.direction("up"); await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.equal(h.el("basicTest").disabled, true);
});

for (const outcome of ["unknown", "pending"])
  test(`${outcome} blocks new directions and recalculation never replays`, async () => {
    const h = harness(); await h.load(); await h.direction("up"); acceptGesture(h, outcome);
    await h.click("basicTest");
    const saved = [...h.storage];
    await h.direction("down"); await h.click("basicRecalculate"); await h.click("basicQuery");
    await h.click("basicTest"); await h.click("basicDoubleTap");
    assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
    assert.deepEqual([...h.storage], saved);
    assert.equal(h.el("basicTest").disabled, true);
    assert.equal(h.el("basicDoubleTap").disabled, true);
    assert.equal(h.el("basicReview").hidden, false);
    assert.ok(h.calls.filter(c => c.url.pathname === "/api/gestures/state").slice(1)
      .every(c => c.url.searchParams.get("requestId") === "ticket-1"));
  });

for (const action of ["double_tap", "swipe"])
  test(`arbitrary ${action} still sends its calculated points with one click`, async () => {
    const h = harness(); await h.load(); await h.manual(action);
    const result = action === "swipe" ? {...h.plan.presets[1], id: "manual",
      start: normalized(.3, .6), end: normalized(.4, .2), durationMs: 865,
      points: {start: cameraPoint(384, 432), end: cameraPoint(512, 144)}} :
      {...h.plan.presets[0], id: "manual", point: normalized(.3, .6),
        points: {point: cameraPoint(384, 432)}};
    h.responder = c => c.url.pathname === "/api/gestures/preview" ? result : undefined;
    h.mark(.3, .6); await h.click("basicStart"); await h.flush();
    if (action === "swipe") { h.mark(.4, .2); await h.click("basicEnd"); await h.flush(); }
    acceptGesture(h); await h.click("basicTest");
    const writes = h.calls.filter(c => c.url.pathname === "/api/gestures/test");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].body.action, action);
    assert.deepEqual(writes[0].body.point || writes[0].body.start, result.point || result.start);
    if (action === "swipe") assert.deepEqual(writes[0].body.end, result.end);
  });

test("bottom-up candidate still executes once through the same guarded path", async () => {
  const h = harness(); await h.load(); await h.change("basicMode", "bottom_up"); acceptGesture(h);
  assert.equal(h.el("basicDirections").hidden, true);
  assert.match(h.el("basicCalculationSummary").textContent, /候选.*未验证/);
  await h.click("basicTest");
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body.start, h.plan.presets[5].start);
  assert.deepEqual(writes[0].body.end, h.plan.presets[5].end);
});

for (const id of ["double_tap_center", "swipe_up", "swipe_down", "swipe_left", "swipe_right", "bottom_up"])
  test(`preset idle for ten seconds refreshes its frame before one POST: ${id}`, async () => {
    const h = harness(); await h.load();
    if (id.startsWith("swipe_")) await h.direction(id.slice(6));
    else if (id === "bottom_up") await h.change("basicMode", "bottom_up");
    const button = id === "double_tap_center" ? "basicDoubleTap" : "basicTest";
    const original = h.value(id === "double_tap_center" ? 'basicPayload("double_tap_center")' : "basicPayload()");
    h.now += 10000; h.plan.frameSeq = 501; h.run("S.camSeq = 501");
    acceptGesture(h);
    const accept = h.responder;
    h.responder = call => {
      if (call.url.pathname === "/api/gestures/test" && h.plan.frameSeq - call.body.frameSeq > 300)
        return {httpError: 400, message: "标记画面已过期，请重新标记"};
      return accept(call);
    };
    const count = h.calls.length;
    await h.click(button);
    const calls = h.calls.slice(count), writes = calls.filter(c => c.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url.pathname, "/api/gestures/test");
    assert.equal(writes[0].body.frameSeq, 501);
    assert.deepEqual({...writes[0].body, frameSeq: original.frameSeq}, original);
    assert.deepEqual(calls.slice(0, 2).map(c => [c.method, c.url.pathname]),
      [["GET", "/api/gestures/plan"], ["POST", "/api/gestures/test"]]);
    assert.equal(calls[0].cache, "no-store");
    assert.equal(calls[0].url.searchParams.get("deviceId"), "arm-01");
    assert.equal(calls[0].url.searchParams.get("contextId"), "ctx-1");
    assert.equal(h.run("BASIC.uncertain"), null);
  });

for (const button of ["basicDoubleTap", "basicTest"])
  test(`preflight locks repeated clicks without marking an action unknown: ${button}`, async () => {
    const h = harness(); await h.load(); await h.direction("up"); acceptGesture(h);
    const fresh = deferred(), accept = h.responder;
    let planReads = 0;
    h.responder = call => call.url.pathname === "/api/gestures/plan" && ++planReads === 1 ?
      fresh.promise : accept(call);
    const count = h.calls.length;
    const click = h.click(button);
    await h.click("basicDoubleTap"); await h.click("basicTest"); await h.click(button);
    assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
      [["GET", "/api/gestures/plan"]]);
    assert.equal(h.run("BASIC.uncertain"), null);
    assert.equal(h.storage.size, 0);
    assert.equal(h.el("basicReview").hidden, true);
    assert.equal(h.el("basicDoubleTap").disabled, true);
    assert.equal(h.el("basicTest").disabled, true);
    assert.match(h.el("basicStatus").textContent, /尚未提交/);
    fresh.resolve(plan({frameSeq: 501})); await click;
    const writes = h.calls.slice(count).filter(c => c.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].body.frameSeq, 501);
    assert.equal(h.run("BASIC.busy"), false);
    assert.equal(h.run("S.busy"), 0);
  });

for (const [label, respond] of [
  ["404", () => ({httpError: 404})],
  ["expired context", () => ({httpError: 409, message: "device_context_expired"})],
  ["network failure", () => { throw new TypeError("NetworkError"); }],
  ["timeout", () => { const error = new Error("TimeoutError"); error.name = "TimeoutError"; throw error; }],
]) test(`preflight ${label} sends no POST, performs no recovery and creates no uncertainty`, async () => {
  const h = harness(); await h.load();
  h.el("basicStatus").textContent = "previous confirmed result";
  const originalState = h.value("BASIC.state"), count = h.calls.length;
  h.responder = call => call.url.pathname === "/api/gestures/plan" ? respond() : undefined;
  await h.click("basicDoubleTap");
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
    [["GET", "/api/gestures/plan"]]);
  assert.deepEqual(h.value("BASIC.state"), originalState);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.run("BASIC.busy"), false);
  assert.equal(h.run("S.busy"), 0);
  assert.equal(h.el("basicReview").hidden, true);
  assert.match(h.el("basicStatus").textContent, /^动作未提交：/);
  assert.doesNotMatch(h.el("basicStatus").textContent, /响应未核实|结果未知|动作进行中/);
});

for (const [label, mutate] of [
  ["device", p => { p.deviceId = "arm-02"; }],
  ["context", p => { p.contextId = "ctx-2"; }],
  ["not ready", p => { p.calibrationReady = false; }],
  ["full calibration", p => {
    p.calibrationFingerprint = p.binding.calibration_fingerprint = "full-calibration-2";
  }],
  ["binding revision", p => { p.binding.binding_revision++; }],
  ["camera", p => { p.binding.camera_device_unique_id = "uvc-02"; }],
  ["daemon session", p => { p.binding.daemon_session = "session-2"; }],
  ["profile", p => { p.profile.tapDepth++; }],
  ["start", p => { p.presets[1].start.x = .4; }],
  ["end", p => { p.presets[1].end.y = .3; }],
  ["camera overlay", p => { p.presets[1].points.start.cameraX++; }],
  ["duration", p => { p.presets[1].durationMs++; }],
  ["missing selected preset", p => { p.presets.splice(1, 1); }],
  ["invalid presets", p => { p.presets = null; }],
  ["global gate", p => { p.blockingReasons = ["new lease"]; }],
  ["action gate", p => { p.actions.swipe.usable = false; }],
  ["item gate", p => { p.presets[1].blockingReasons = ["unsafe path"]; }],
  ["invalid frame", p => { p.frameSeq = null; }],
  ["string frame", p => { p.frameSeq = "501"; }],
  ["frame rollback", p => { p.frameSeq = 99; }],
]) test(`preflight rejects changed ${label} before submission`, async () => {
  const h = harness(); await h.load(); await h.direction("up");
  const originalPlan = h.value("BASIC.plan"), originalCalculation = h.value("BASIC.calculation");
  h.plan.frameSeq = 501; mutate(h.plan);
  const count = h.calls.length;
  await h.click("basicTest");
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
    [["GET", "/api/gestures/plan"]]);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.run("BASIC.state.requestId"), "ticket-1");
  assert.deepEqual(h.value("BASIC.plan"), originalPlan);
  assert.deepEqual(h.value("BASIC.calculation"), originalCalculation);
  assert.match(h.el("basicStatus").textContent, /^动作未提交：/);
});

for (const [label, mutate] of [
  ["center point", p => { p.presets[0].point.x = .4; }],
  ["double-tap interval", p => { p.presets[0].intervalMs = 150; }],
  ["center action", p => { p.presets[0].action = "swipe"; }],
]) test(`double-tap preflight rejects changed ${label}`, async () => {
  const h = harness(); await h.load(); mutate(h.plan);
  await h.click("basicDoubleTap");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  assert.equal(h.storage.size, 0);
  assert.match(h.el("basicStatus").textContent, /^动作未提交：/);
});

for (const [label, change] of [
  ["ticket", h => h.run('BASIC.state.requestId = "ticket-2"')],
  ["ticket binding", h => h.run('BASIC.state.binding.zHash = "z-2"')],
  ["calibration", h => h.run('BASIC.bundleKey = "new bundle"')],
  ["not ready", h => h.run("BASIC.bundleReady = false")],
  ["selection", h => h.direction("down")],
  ["selection round trip", async h => { await h.direction("down"); await h.direction("up"); }],
  ["mode", h => h.change("basicMode", "manual")],
  ["context", h => h.run('S.contextId = "ctx-2"')],
  ["camera", h => h.run('S.context.cameraId = "cam-02"')],
  ["device generation", h => h.run("selectionGeneration++")],
  ["calculation", h => h.run("BASIC.calculation.item.start.x = .4")],
  ["local plan point", h => h.run("BASIC.plan.presets[1].start.x = .4")],
  ["state reload", h => h.run("BASIC.stateLoad++")],
  ["external motion", h => h.run("S.busy++")],
  ["backend block", h => h.run("BASIC.state.blocked = true")],
]) test(`preflight rejects concurrent ${label} without renewing the ticket`, async () => {
  const h = harness(); await h.load(); await h.direction("up");
  const pending = deferred();
  h.responder = call => call.url.pathname === "/api/gestures/plan" ? pending.promise : undefined;
  const count = h.calls.length, click = h.click("basicTest");
  await change(h);
  pending.resolve(plan({frameSeq: 501})); await click;
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
    [["GET", "/api/gestures/plan"]]);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.run("BASIC.busy"), false);
});

test("uncertainty arriving during preflight is preserved and prevents submission", async () => {
  const h = harness(); await h.load();
  const pending = deferred();
  h.responder = call => call.url.pathname === "/api/gestures/plan" ? pending.promise : undefined;
  const click = h.click("basicDoubleTap");
  h.run('BASIC.uncertain = {requestId:"other-unknown", session:"session-1"}');
  h.storage.set("basic-gesture-pending:arm-01", JSON.stringify(h.value("BASIC.uncertain")));
  const stored = [...h.storage];
  pending.resolve(plan({frameSeq: 501})); await click;
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  assert.equal(h.run("BASIC.uncertain.requestId"), "other-unknown");
  assert.deepEqual([...h.storage], stored);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.equal(h.el("basicReview").hidden, false);
});

for (const action of ["double_tap", "swipe"])
  test(`manual ${action} refreshes frame only after the immutable normalized plan passes preflight`, async () => {
    const h = harness(); await h.load(); await h.manual(action);
    h.responder = call => call.url.pathname === "/api/gestures/preview" ?
      {...h.plan.presets[action === "swipe" ? 1 : 0], id: "manual"} : undefined;
    h.mark(.5, .75); await h.click("basicStart"); await h.flush();
    if (action === "swipe") { h.mark(.5, .25); await h.click("basicEnd"); await h.flush(); }
    const original = h.value("basicPayload()");
    h.now += 10000; h.plan.frameSeq = 501; h.run("S.camSeq = 501");
    acceptGesture(h);
    const count = h.calls.length;
    await h.click("basicTest");
    const calls = h.calls.slice(count), writes = calls.filter(c => c.method === "POST");
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0].body, {...original, frameSeq: 501});
    assert.deepEqual(calls.slice(0, 2).map(c => [c.method, c.url.pathname]),
      [["GET", "/api/gestures/plan"], ["POST", "/api/gestures/test"]]);
    assert.equal(h.state.lastResult.outcome, "confirmed");
    assert.equal(h.el("basicTest").disabled, false);
    assert.deepEqual(h.value("basicPayload().point || basicPayload().start"), original.point || original.start);
    assert.equal(h.value("basicPayload().requestId"), "ticket-2");
    assert.equal(h.run("BASIC.uncertain"), null);
  });

test("motion_ready calibration remains usable for preset preflight", async () => {
  const h = harness(); await h.load();
  h.put("wizard", {bundle: {status: "motion_ready", z: {tapDepth: 3}}});
  await h.run("syncBasicCalibration(wizard)");
  assert.equal(h.run("BASIC.bundleReady"), true);
  assert.equal(h.el("basicDoubleTap").disabled, false);
  acceptGesture(h); h.plan.frameSeq = 501;
  await h.click("basicDoubleTap");
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.frameSeq, 501);
});

for (const interval of [50, 120, 280, 500])
  test(`center double-tap submits the actual ${interval} ms input as one macro`, async () => {
    const h = harness(); await h.load();
    const planBefore = h.value("BASIC.plan");
    h.el("basicInterval").value = String(interval); await h.el("basicInterval").dispatch("input");
    assert.equal(h.el("basicDoubleTap").disabled, false);
    acceptGesture(h); h.plan.frameSeq = 501;
    await h.click("basicDoubleTap");
    const writes = h.calls.filter(c => c.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url.pathname, "/api/gestures/test");
    assert.equal(writes[0].body.action, "double_tap");
    assert.equal(writes[0].body.intervalMs, interval);
    assert.equal(writes[0].body.frameSeq, 501);
    assert.equal(writes[0].body.requestId, "ticket-1");
    assert.deepEqual(writes[0].body.binding, state().binding);
    assert.doesNotMatch(JSON.stringify(writes[0].body), /tapDepth|tapFeed|hoverZ|"feed"|durationMs/);
    assert.deepEqual(h.value("BASIC.plan.profile"), planBefore.profile);
    assert.equal(h.run("BASIC.plan.presets[0].intervalMs"), 120);
    assert.equal(h.el("basicInterval").value, String(interval));
  });

for (const input of ["", " ", "NaN", "Infinity", "49", "501", "120.5", "125"])
  test(`invalid double-tap interval ${JSON.stringify(input)} blocks requests, not swipes`, async () => {
    const h = harness(); await h.load(); await h.direction("up");
    h.el("basicInterval").value = input; await h.el("basicInterval").dispatch("input");
    assert.equal(h.el("basicDoubleTap").disabled, true);
    assert.equal(h.el("basicTest").disabled, false);
    const count = h.calls.length;
    await h.click("basicDoubleTap");
    assert.equal(h.calls.length, count);
    assert.equal(h.run("BASIC.uncertain"), null);
    assert.equal(h.storage.size, 0);
    assert.match(h.messages.at(-1).message, /两次点击间隔/);
    acceptGesture(h); await h.click("basicTest");
    const writes = h.calls.filter(c => c.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].body.action, "swipe");
    assert.equal(writes[0].body.durationMs, 740);
    assert.equal("intervalMs" in writes[0].body, false);
    assert.doesNotMatch(JSON.stringify(writes[0].body), /tapDepth|tapFeed|hoverZ|"feed"/);
  });

test("interval edits persist through rendering, direction changes and plan refreshes", async () => {
  const h = harness(); await h.load();
  h.el("basicInterval").value = "280"; await h.el("basicInterval").dispatch("input");
  h.run("renderBasic(); renderBasic()");
  await h.direction("left");
  assert.equal(h.el("basicIntervalLabel").hidden, false);
  await h.click("basicRecalculate");
  assert.equal(h.el("basicInterval").value, "280");
  await h.manual("double_tap");
  assert.equal(h.el("basicIntervalLabel").hidden, false);
  assert.equal(h.el("basicInterval").value, "280");
  await h.manual("swipe");
  assert.equal(h.el("basicIntervalLabel").hidden, true);
  await h.change("basicMode", "preset");
  assert.equal(h.el("basicInterval").value, "280");
});

test("manual double-tap interval edit preserves marks; execution performs the common frame preflight", async () => {
  const h = harness(); await h.load(); await h.manual("double_tap");
  h.responder = call => call.url.pathname === "/api/gestures/preview" ?
    {...h.plan.presets[0], id: "manual", point: normalized(.3, .6),
      points: {point: cameraPoint(384, 432)}} : undefined;
  h.mark(.3, .6); await h.click("basicStart"); await h.flush();
  const calculation = h.value("BASIC.calculation"), marks = h.value("BASIC.marks");
  const count = h.calls.length;
  h.el("basicInterval").value = "350"; await h.el("basicInterval").dispatch("input");
  assert.deepEqual(h.value("BASIC.calculation"), calculation);
  assert.deepEqual(h.value("BASIC.marks"), marks);
  assert.equal(h.calls.length, count);
  acceptGesture(h); await h.click("basicTest");
  const writes = h.calls.slice(count).filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url.pathname, "/api/gestures/test");
  assert.equal(writes[0].body.action, "double_tap");
  assert.equal(writes[0].body.intervalMs, 350);
  assert.equal(writes[0].body.frameSeq, 100);
  assert.deepEqual(writes[0].body.point, normalized(.3, .6));
});

test("changing interval during preflight refuses submission without uncertainty", async () => {
  const h = harness(); await h.load();
  const pending = deferred();
  h.responder = call => call.url.pathname === "/api/gestures/plan" ? pending.promise : undefined;
  const count = h.calls.length, click = h.click("basicDoubleTap");
  assert.equal(h.el("basicInterval").disabled, true);
  h.el("basicInterval").value = "280"; await h.el("basicInterval").dispatch("input");
  pending.resolve(plan({frameSeq: 501})); await click;
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
    [["GET", "/api/gestures/plan"]]);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.el("basicInterval").disabled, false);
  assert.match(h.el("basicStatus").textContent, /^动作未提交：/);
});

test("editing the interval never clears an unknown double-tap result or replays the macro", async () => {
  const h = harness(); await h.load(); acceptGesture(h, "unknown");
  await h.click("basicDoubleTap");
  const stored = [...h.storage], originalResult = h.value("BASIC.state.lastResult");
  h.el("basicInterval").value = "280"; await h.el("basicInterval").dispatch("input");
  await h.click("basicDoubleTap"); await h.click("basicRecalculate"); await h.click("basicDoubleTap");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
  assert.deepEqual([...h.storage], stored);
  assert.deepEqual(h.value("BASIC.state.lastResult"), originalResult);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.equal(h.el("basicReview").hidden, false);
});

test("exact default workflow: no operator, one camera click previews that point and enables only arbitrary double tap", async () => {
  const h = harness({defaultView: true}); await h.load();
  assert.equal(h.run('$("#basicMode").value'), "manual");
  assert.equal(h.run("basicAction()"), "double_tap");
  assert.equal(h.el("basicActions").hidden, false);
  assert.equal(h.el("basicDoubleTap").hidden, true);
  assert.equal(h.el("basicDoubleTapCommand").hidden, true);
  assert.equal(h.el("basicStart").hidden, true);
  assert.equal(h.el("basicEnd").hidden, true);
  assert.equal(h.el("basicTest").disabled, true);
  assert.match(h.el("basicTestReason").textContent, /点选画面/);
  h.responder = c => previewGesture(h, c);
  await h.draw([256, 432]);
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.el("basicTestReason").textContent, "");
  assert.equal(h.el("basicDoubleTapCommand").hidden, true);
  assert.deepEqual(h.value("basicPayload().point"), normalized(.2, .6));
  assert.notDeepEqual(h.value("basicPayload().point"), h.plan.presets[0].point);
  assert.deepEqual(h.value("[S.lastMark.nx,S.lastMark.ny]"), [.2, .6]);
  assert.equal(h.run("S.lastMark.basicRequestId"), "ticket-1");
  assert.deepEqual(h.calls.filter(c => c.method === "POST").map(c => c.url.pathname), ["/api/gestures/preview"]);
  acceptGesture(h); await h.click("basicTest");
  const writes = h.calls.filter(c => c.url.pathname === "/api/gestures/test");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body.point, normalized(.2, .6));
  assert.equal(writes[0].body.operator, "调试台");
});

test("click-only camera events also select a double tap and retain the legacy mark", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => previewGesture(h, c);
  await h.el("camwrap").dispatch("click", {clientX: 256, clientY: 432});
  await h.flush();
  assert.equal(h.el("basicTest").disabled, false);
  assert.deepEqual(h.value("basicPayload().point"), normalized(.2, .6));
  assert.equal(h.run("S.lastMark.nx"), .2);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
});

test("default mode preserves calibration point marking before gesture calibration is ready", async () => {
  const h = harness({defaultView: true});
  h.plan.calibrationReady = false; h.plan.presets = [];
  await h.load(); await h.draw([256, 432]);
  assert.equal(h.run("S.lastMark.nx"), .2);
  assert.equal(h.run("S.lastMark.ny"), .6);
  assert.equal(h.run("BASIC.calculation"), null);
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

for (const points of [
  [[0, 360], [800, 360]], [[640, 720], [640, 144]], [[1279, 360], [0, 360]],
]) test(`direct swipe preserves drawn edges and previews once: ${JSON.stringify(points)}`, async () => {
  const h = harness({defaultView: true}); await h.load(); await h.manual("swipe");
  h.responder = c => previewGesture(h, c);
  await h.draw(...points);
  const calls = h.calls.filter(c => c.method === "POST");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url.pathname, "/api/gestures/preview");
  assert.deepEqual(calls[0].body.start, cameraPoint(...points[0]));
  assert.deepEqual(calls[0].body.end, cameraPoint(...points[1]));
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.el("basicSwap").disabled, false);
  assert.equal(h.el("basicEndValue").hidden, false);
  assert.equal(h.run("S.lastMark.nx"), points[1][0] / 1280);
  const payload = h.value("basicPayload()");
  assert.deepEqual(payload.start, normalized(points[0][0] / 1280, points[0][1] / 720));
  assert.deepEqual(payload.end, normalized(points[1][0] / 1280, points[1][1] / 720));
});

test("unsafe edge preview failure retains visible marks but never enables execution or save", async () => {
  const h = harness({defaultView: true}); await h.load(); await h.manual("swipe");
  h.responder = c => c.url.pathname === "/api/gestures/preview" ?
    {httpError: 400, message: "滑动路径超出校准安全边界"} : undefined;
  await h.draw([0, 720], [640, 144]);
  assert.equal(h.run("BASIC.marks.start.cameraX"), 0);
  assert.equal(h.run("BASIC.marks.start.cameraY"), 720);
  assert.equal(h.el("basicTest").disabled, true);
  h.el("basicSavedLabel").value = "边缘"; h.run("renderBasic()");
  assert.equal(h.el("basicSave").disabled, true);
  assert.match(h.el("basicTest").title, /安全边界/);
  assert.match(h.el("basicTestReason").textContent, /安全边界/);
  await h.click("basicTest"); await h.click("basicSave");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
});

for (const [label, change] of [
  ["pointer cancellation", h => h.pointer("pointercancel", 500, 250)],
  ["lost pointer capture", h => h.pointer("lostpointercapture", 500, 250)],
  ["ticket change", h => { h.state.requestId = "ticket-2"; return h.load(); }],
  ["binding change", h => h.run("S.context.bindingRevision++")],
  ["mode change", h => h.change("basicMode", "preset")],
]) test(`interrupted draw cannot preview or execute: ${label}`, async () => {
  const h = harness({defaultView: true}); await h.load(); await h.manual("swipe");
  await h.pointer("pointerdown", 640, 540);
  await h.pointer("pointermove", 500, 250);
  await change(h);
  await h.pointer("pointerup", 500, 250);
  await h.el("camwrap").dispatch("click", {clientX: 500, clientY: 250});
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("drag ending outside image cancels instead of clipping to an executable edge", async () => {
  const h = harness({defaultView: true}); await h.load(); await h.manual("swipe");
  await h.draw([640, 540], [640, -1]);
  assert.equal(h.run("BASIC.calculation"), null);
  assert.deepEqual(h.value("BASIC.marks"), {});
  assert.match(h.el("basicTestReason").textContent, /画面内/);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("changing legacy camera mode remains explicit and never turns the prior draw into motion", async () => {
  const h = harness({defaultView: true}); await h.load();
  await h.pointer("pointerdown", 256, 432);
  await h.run(`(async () => {
    const input = $('input[name="camMode"][value="tap"]');
    input.checked = true; await input.dispatch("change");
  })()`);
  await h.pointer("pointerup", 256, 432);
  await h.el("camwrap").dispatch("click", {clientX: 256, clientY: 432});
  assert.equal(h.run('$("#basicMode").value'), "preset");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("overlay draws start, end and an arrow at the selected image coordinates", async () => {
  const h = harness(); await h.load(); await h.manual("swipe");
  h.responder = c => previewGesture(h, c);
  await h.draw([640, 700], [320, 140]);
  const segments = [], labels = [];
  h.run('const camImg = $("#camimg"), camOvl = $("#camovl"); camOvl.width = 1280; camOvl.height = 720;');
  h.run(section("function toCanvas(", '$("#btnDetect")'));
  const context = {clearRect() {}, beginPath() {}, stroke() {}, strokeRect() {}, arc() {},
    fill() {}, fillText: text => labels.push(text), moveTo: (x,y) => segments.push(["M",x,y]),
    lineTo: (x,y) => segments.push(["L",x,y])};
  h.run("globalThis.drawContext = null");
  h.el("camovl").getContext = () => context;
  h.run('drawOverlays($("#camovl").getContext("2d"))');
  assert.ok(labels.includes("起点")); assert.ok(labels.includes("终点"));
  const index = segments.findIndex(s => s[0] === "M" && s[1] === 640 && s[2] === 700);
  assert.deepEqual(segments.slice(index, index + 2), [["M", 640, 700], ["L", 320, 140]]);
  assert.deepEqual(segments[index + 3], ["L", 320, 140]);
});

for (const [label, setup] of [
  ["unknown", h => {
    h.state = state({requestId: null, blocked: true, blockingReasons: ["原结果未知，禁止动作"],
      lastResult: {requestId: "unknown-1", outcome: "unknown", message: "原结果未知"}});
    h.storage.set("basic-gesture-pending:arm-01", JSON.stringify({requestId: "unknown-1", session: "session-1"}));
  }],
  ["foreign lease", h => {
    h.plan.blockingReasons = ["设备被其他任务占用"];
    h.plan.actions.double_tap = {usable: false, blockingReasons: ["设备被其他任务占用"]};
  }],
]) test(`motion blocker permits read-only drawing and saving: ${label}`, async () => {
  const h = harness({defaultView: true}); setup(h); await h.load();
  const result = h.value("BASIC.state.lastResult"), stored = [...h.storage];
  h.responder = c => {
    if (c.url.pathname === "/api/gestures/saved" && c.method === "POST")
      return savedEntry(h.value("BASIC.calculation.item"), {label: c.body.label, interval: c.body.interval_ms});
    return previewGesture(h, c);
  };
  await h.draw([256, 432]);
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.run("BASIC.calculation.item.calculated"), true);
  h.el("basicSavedLabel").value = "返回桌面"; h.run("renderBasic()");
  assert.equal(h.el("basicSave").disabled, false);
  await h.click("basicSave"); await h.click("basicTest");
  assert.deepEqual(h.calls.filter(c => c.method === "POST").map(c => c.url.pathname),
    ["/api/gestures/preview", "/api/gestures/saved"]);
  assert.match(h.el("basicSavedStatus").textContent, /已保存，未执行动作/);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.deepEqual([...h.storage], stored);
});

test("an explicit state query still invalidates drawn marks when it replaces the ticket", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => previewGesture(h, c);
  await h.draw([256, 432]);
  h.now += 60000; h.run("renderBasic()");
  const count = h.calls.length;
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.calls.length, count);
  h.state.requestId = "ticket-2";
  await h.click("basicQuery");
  assert.equal(h.run("BASIC.calculation"), null);
  assert.equal(h.run("S.lastMark"), null);
  assert.deepEqual(h.value("BASIC.marks"), {});
  await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 0);
});

async function drawnGesture(action) {
  const h = harness({defaultView: true}); await h.load(); await h.manual(action);
  h.responder = call => previewGesture(h, call);
  await h.draw([256, 432], action === "swipe" ? [640, 144] : [256, 432]);
  if (action === "double_tap") {
    h.el("basicInterval").value = "280"; await h.el("basicInterval").dispatch("input");
  }
  return h;
}

for (const action of ["double_tap", "swipe"])
  for (const when of ["before click", "during plan read"])
    for (const outcome of ["confirmed", "executed"])
      test(`expired ${action} refreshes once with identical geometry and retains marks: ${when}, ${outcome}`, async () => {
        const h = await drawnGesture(action);
        const original = h.value("basicPayload()"), marks = h.value("BASIC.marks");
        const item = h.value("BASIC.calculation.item"), count = h.calls.length;
        const pending = deferred();
        h.plan.frameSeq = 501;
        acceptGesture(h, outcome);
        const accept = h.responder;
        let reads = 0, submissions = 0;
        h.responder = call => {
          if (call.url.pathname === "/api/gestures/plan" && ++reads === 1 && when === "during plan read")
            return pending.promise;
          if (call.url.pathname === "/api/gestures/state" && submissions === 0)
            return state({requestId: "renewed-ticket"});
          const result = accept(call);
          if (call.url.pathname === "/api/gestures/test")
            h.state.requestId = `ready-${++submissions}`;
          return result;
        };
        if (when === "before click") h.advance(60000);
        assert.equal(h.el("basicTest").disabled, false);
        assert.equal(h.calls.length, count, "Expiry alone must not fetch or execute");
        const run = h.click("basicTest");
        if (when === "during plan read") {
          await h.flush(); h.advance(60000);
          assert.equal(h.calls.slice(count).filter(c => c.method === "POST").length, 0);
          pending.resolve(clone(h.plan));
        }
        await run;
        const calls = h.calls.slice(count), writes = calls.filter(c => c.method === "POST");
        assert.equal(writes.length, 1);
        assert.deepEqual(calls.slice(0, 3).map(c => [c.method, c.url.pathname]),
          [["GET", "/api/gestures/plan"], ["GET", "/api/gestures/state"], ["POST", "/api/gestures/test"]]);
        assert.equal(calls[1].cache, "no-store");
        assert.equal(calls[1].url.searchParams.get("deviceId"), "arm-01");
        assert.equal(calls[1].url.searchParams.get("contextId"), "ctx-1");
        assert.equal(calls[1].url.searchParams.has("requestId"), false);
        assert.deepEqual(writes[0].body, {...original, requestId: "renewed-ticket", frameSeq: 501});
        assert.deepEqual(h.value("BASIC.marks"), marks);
        assert.deepEqual(h.value("BASIC.calculation.item"), item);
        assert.equal(h.el("basicTest").disabled, false);
        assert.equal(h.run("BASIC.state.lastResult.outcome"), outcome);
        assert.equal(h.run("BASIC.uncertain"), null);
        assert.equal(h.storage.size, 0);
        const completedCalls = h.calls.length;
        await h.flush();
        assert.equal(h.calls.length, completedCalls, "Completion must not repeat the action");
        await h.click("basicTest");
        const allWrites = h.calls.slice(count).filter(c => c.method === "POST");
        assert.equal(allWrites.length, 2, "Each explicit click gets exactly one POST");
        assert.deepEqual(allWrites[1].body, {...original, requestId: "ready-1", frameSeq: 501});
        assert.deepEqual(h.value("BASIC.marks"), marks);
        assert.deepEqual(h.value("BASIC.calculation.item"), item);
        assert.equal(h.el("basicTest").disabled, false);
        assert.equal(h.calls.slice(count).filter(c => c.url.pathname === "/api/gestures/preview").length, 0);
      });

const invalidRenewals = [
  ["blocked", fresh => { fresh.blocked = true; fresh.blockingReasons = ["new lease"]; }],
  ["missing blocked flag", fresh => { delete fresh.blocked; }],
  ["nonboolean blocked flag", fresh => { fresh.blocked = 0; }],
  ["foreign device", fresh => { fresh.deviceId = "arm-02"; }],
  ["foreign context", fresh => { fresh.contextId = "ctx-2"; }],
  ["missing ticket", fresh => { delete fresh.requestId; }],
  ["empty ticket", fresh => { fresh.requestId = ""; }],
  ["nonstring ticket", fresh => { fresh.requestId = 123; }],
  ["consumed ticket", fresh => { fresh.lastResult = {requestId: fresh.requestId, outcome: "confirmed"}; }],
  ["missing binding", fresh => { fresh.binding = null; }],
  ...["geometryHash", "zHash", "screenCalibrationFingerprint", "daemonSession", "cameraUniqueId"]
    .map(key => [`changed ${key}`, fresh => { fresh.binding[key] = "changed"; }]),
  ["changed binding revision", fresh => { fresh.binding.bindingRevision++; }],
  ["changed frame", fresh => { fresh.binding.frame.width++; }],
  ...[0, -1, null, false, "60000", NaN, Infinity].map(ttl =>
    [`invalid TTL ${String(ttl)}`, fresh => { fresh.expiresInMs = ttl; }]),
  ["missing TTL", fresh => { delete fresh.expiresInMs; }],
  ...["unknown", "pending"].map(outcome => [`unreviewed ${outcome}`, fresh => {
    fresh.lastResult = {requestId: "prior-unknown", outcome, message: "prior result unresolved"};
  }]),
];
for (const action of ["double_tap", "swipe"])
  for (const [label, mutate] of invalidRenewals)
    test(`expired ${action} rejects fresh ${label} without POST or local renewal`, async () => {
      const h = await drawnGesture(action);
      const snapshot = h.value("[BASIC.state, BASIC.calculation, BASIC.marks]"), stored = [...h.storage];
      const fresh = state({requestId: "renewed-ticket"}); mutate(fresh);
      h.responder = call => call.url.pathname === "/api/gestures/state" ? fresh : undefined;
      h.advance(60000);
      const expires = h.run("BASIC.expires"), count = h.calls.length;
      await h.click("basicTest");
      assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
        [["GET", "/api/gestures/plan"], ["GET", "/api/gestures/state"]]);
      assert.deepEqual(h.value("[BASIC.state, BASIC.calculation, BASIC.marks]"), snapshot);
      assert.equal(h.run("BASIC.expires"), expires);
      assert.equal(h.run("BASIC.uncertain"), null);
      assert.deepEqual([...h.storage], stored);
      assert.equal(h.run("BASIC.busy"), false);
      assert.equal(h.run("S.busy"), 0);
      assert.match(h.el("basicStatus").textContent, /^动作未提交/);
    });

for (const [label, fail] of [
  ["HTTP 503", () => ({httpError: 503})],
  ["expired context", () => ({httpError: 410, message: "device_context_expired"})],
  ["network failure", () => { throw new TypeError("NetworkError"); }],
  ["timeout", () => { const error = new Error("TimeoutError"); error.name = "TimeoutError"; throw error; }],
]) test(`ticket refresh ${label} never retries, recovers context or manufactures unknown`, async () => {
  const h = await drawnGesture("double_tap");
  const snapshot = h.value("[BASIC.state, BASIC.calculation, BASIC.marks]");
  h.responder = call => call.url.pathname === "/api/gestures/state" ? fail() : undefined;
  h.advance(60000);
  const count = h.calls.length;
  await h.click("basicTest"); await h.flush();
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
    [["GET", "/api/gestures/plan"], ["GET", "/api/gestures/state"]]);
  assert.deepEqual(h.value("[BASIC.state, BASIC.calculation, BASIC.marks]"), snapshot);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.run("BASIC.busy"), false);
  assert.equal(h.run("S.busy"), 0);
  assert.match(h.el("basicStatus").textContent, /^动作未提交/);
});

for (const action of ["double_tap", "swipe"])
  test(`ticket refresh locks duplicate ${action} clicks before creating uncertainty`, async () => {
    const h = await drawnGesture(action);
    h.advance(60000);
    acceptGesture(h);
    const accept = h.responder, pending = deferred();
    let submitted = false;
    h.responder = call => {
      if (call.url.pathname === "/api/gestures/state" && !submitted) return pending.promise;
      if (call.url.pathname === "/api/gestures/test") submitted = true;
      return accept(call);
    };
    const count = h.calls.length, run = h.click("basicTest");
    await h.flush();
    await h.click("basicTest"); await h.click("basicTest");
    assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
      [["GET", "/api/gestures/plan"], ["GET", "/api/gestures/state"]]);
    assert.equal(h.run("BASIC.uncertain"), null);
    assert.equal(h.storage.size, 0);
    assert.equal(h.el("basicTest").disabled, true);
    pending.resolve(state({requestId: "renewed-ticket"})); await run;
    assert.equal(h.calls.slice(count).filter(c => c.method === "POST").length, 1);
    assert.equal(h.el("basicTest").disabled, false);
  });

for (const action of ["double_tap", "swipe"])
  test(`unknown arriving during ${action} ticket refresh is never cleared or submitted`, async () => {
    const h = await drawnGesture(action), pending = deferred();
    h.advance(60000);
    h.responder = call => call.url.pathname === "/api/gestures/state" ? pending.promise : undefined;
    const count = h.calls.length, run = h.click("basicTest");
    await h.flush();
    h.run('BASIC.uncertain = {requestId:"other-unknown", session:"session-1"};' +
      'BASIC.state.lastResult = {requestId:"other-unknown", outcome:"unknown", message:"original unknown"};' +
      'showBasicResult(BASIC.state.lastResult)');
    h.storage.set("basic-gesture-pending:arm-01", JSON.stringify(h.value("BASIC.uncertain")));
    const snapshot = h.value("[BASIC.state, BASIC.uncertain]"), stored = [...h.storage];
    const evidence = [...h.el("basicEvidence").children];
    pending.resolve(state({requestId: "renewed-ticket"})); await run;
    assert.equal(h.calls.slice(count).filter(c => c.method === "POST").length, 0);
    assert.deepEqual(h.value("[BASIC.state, BASIC.uncertain]"), snapshot);
    assert.deepEqual([...h.storage], stored);
    assert.deepEqual(h.el("basicEvidence").children, evidence);
    assert.equal(h.el("basicTest").disabled, true);
    assert.equal(h.el("basicReview").hidden, false);
  });

test("reviewed unknown is history, not an active blocker, without requesting an operator", async () => {
  const h = harness({defaultView: true});
  h.state.lastResult = {requestId: "unknown-1", outcome: "unknown", message: "原结果未知",
    review: {reason: "人工已核对", operator: "调试台"}};
  h.storage.set("basic-gesture-pending:arm-01", JSON.stringify({requestId: "unknown-1", session: "session-1"}));
  await h.load();
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.el("basicReview").hidden, true);
  assert.match(h.el("basicStatus").textContent, /原结果未知[\s\S]*原结果保留/);
  h.responder = c => previewGesture(h, c);
  await h.draw([256, 432]);
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.run("BASIC.state.lastResult.outcome"), "unknown");
  assert.equal(h.storage.size, 0);
});

test("manual review submits only the standard identity and required reason, never motion", async () => {
  const h = harness({defaultView: true});
  h.state.lastResult = {requestId: "unknown-1", outcome: "unknown", message: "原结果未知"};
  await h.load(); await h.click("basicReview");
  assert.equal(h.el("basicReviewDialog").open, true);
  await h.click("basicReviewConfirm");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
  h.el("basicReviewReason").value = "人工检查后结束原试验";
  h.responder = c => {
    if (c.url.pathname !== "/api/gestures/review") return undefined;
    h.state.lastResult.review = {operator: c.body.operator, reason: c.body.reason};
    return clone(h.state.lastResult);
  };
  await h.click("basicReviewConfirm");
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1); assert.equal(writes[0].url.pathname, "/api/gestures/review");
  assert.equal(writes[0].body.operator, "调试台");
  assert.equal(writes[0].body.reason, "人工检查后结束原试验");
  assert.equal(h.run("BASIC.state.lastResult.outcome"), "unknown");
});

for (const interval of [80, 280])
  test(`save/load exact ${interval} ms double tap with reusable ID/version and no motion`, async () => {
    const h = harness({defaultView: true}); await h.load();
    h.responder = c => previewGesture(h, c);
    await h.draw([256, 432]);
    const originalItem = h.value("BASIC.calculation.item");
    h.el("basicInterval").value = String(interval);
    h.el("basicSavedLabel").value = "返回桌面"; h.run("renderBasic()");
    h.responder = c => {
      if (c.url.pathname !== "/api/gestures/saved" || c.method !== "POST") return undefined;
      const entry = savedEntry(originalItem, {label: c.body.label, interval: c.body.interval_ms});
      h.saved = [entry]; return clone(entry);
    };
    await h.click("basicSave");
    const call = h.calls.find(c => c.url.pathname === "/api/gestures/saved" && c.method === "POST");
    assert.deepEqual(call.body, {deviceId: "arm-01", contextId: "ctx-1", label: "返回桌面",
      action: "double_tap", operator: "调试台", binding: h.plan.binding, point: normalized(.2,.6), interval_ms: interval});
    assert.notDeepEqual(call.body.binding, h.state.binding);
    assert.match(h.el("basicSavedStatus").textContent, /编号：gesture-1 · 版本：1/);
    assert.match(h.el("basicSavedStatus").textContent, /手机效果未验证/);
    await h.change("basicMode", "preset");
    await h.change("basicSaved", "gesture-1");
    assert.equal(h.run('$("#basicMode").value'), "manual");
    assert.equal(h.el("basicDoubleTapCommand").hidden, true);
    assert.equal(h.el("basicTest").disabled, false);
    assert.equal(h.el("basicInterval").value, String(interval));
    assert.deepEqual(h.value("basicPayload().point"), normalized(.2, .6));
    assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 0);
    h.plan.frameSeq = 901; h.now += 10000;
    acceptGesture(h); await h.click("basicTest");
    const writes = h.calls.filter(c => c.url.pathname === "/api/gestures/test");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].body.intervalMs, interval);
    assert.equal(writes[0].body.frameSeq, 901);
    assert.deepEqual(writes[0].body.point, normalized(.2, .6));
    assert.doesNotMatch(JSON.stringify(writes[0].body), /gesture_id|tapDepth|tapFeed|hoverZ/);
  });

test("saving a named swipe preserves start/end and versioned updates without semantic binding", async () => {
  const h = harness({defaultView: true}); await h.load(); await h.manual("swipe");
  h.responder = c => previewGesture(h, c);
  await h.draw([100, 690], [640, 144]);
  const item = h.value("BASIC.calculation.item");
  h.el("basicSavedLabel").value = "返回桌面";
  h.responder = c => c.url.pathname === "/api/gestures/saved" && c.method === "POST" ?
    savedEntry(item, {label: c.body.label, version: c.body.expected_version ? c.body.expected_version + 1 : 1}) : undefined;
  await h.click("basicSave");
  h.el("basicSavedLabel").value = "返回上一级";
  await h.click("basicSave");
  const writes = h.calls.filter(c => c.url.pathname === "/api/gestures/saved");
  assert.equal(writes.length, 2);
  assert.equal(writes[1].body.gesture_id, "gesture-1");
  assert.equal(writes[1].body.expected_version, 1);
  assert.equal(writes[1].body.label, "返回上一级");
  assert.deepEqual(writes[1].body.start, item.start);
  assert.deepEqual(writes[1].body.end, item.end);
  assert.equal(writes[1].body.duration_ms, undefined);
  assert.doesNotMatch(JSON.stringify(writes), /navigation_profile|home_action|back_action|tapDepth|tapFeed/);
  assert.match(h.el("basicSavedStatus").textContent, /版本：2/);
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 0);
});

for (const [label, mutate] of [
  ["foreign binding", entry => { entry.binding.binding_revision++; }],
  ["bad point", entry => { entry.plan.point.x = 1.2; }],
  ["mismatched payload", entry => { entry.payload.point.y = .3; }],
  ["unsafe geometry", entry => { entry.plan.blockingReasons = ["unsafe point"]; }],
  ["unusable", entry => { entry.usable = false; entry.blocking_reasons = ["绑定失效"]; }],
  ["verified claim", entry => { entry.effect_status = "confirmed"; }],
  ["missing plan", entry => { entry.plan = null; }],
  ["version changed", entry => { entry.version++; }],
]) test(`saved selection fails closed: ${label}`, async () => {
  const h = harness({defaultView: true}); await h.load();
  h.saved = [savedEntry(h.plan.presets[0])];
  await h.click("basicSavedRefresh");
  mutate(h.saved[0]);
  await h.change("basicSaved", "gesture-1");
  assert.equal(h.run("BASIC.calculation"), null);
  assert.equal(h.el("basicTest").disabled, true);
  assert.match(h.el("basicSavedStatus").textContent, /未载入/);
  await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("saved list failure is visible and cannot fabricate a selected plan", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => c.url.pathname === "/api/gestures/saved" ? {} : undefined;
  await h.click("basicSavedRefresh");
  assert.match(h.el("basicSavedStatus").textContent, /读取失败/);
  assert.equal(h.run("BASIC.calculation"), null);
  assert.equal(h.el("basicTest").disabled, true);
});

test("duplicate save clicks are locked; an unconfirmed receipt never creates motion uncertainty", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => previewGesture(h, c); await h.draw([256, 432]);
  h.el("basicSavedLabel").value = "列表上滑";
  const pending = deferred();
  h.responder = c => c.url.pathname === "/api/gestures/saved" ? pending.promise : undefined;
  const save = h.click("basicSave");
  await h.click("basicSave"); await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/saved").length, 1);
  assert.equal(h.el("basicSave").disabled, true);
  pending.reject(new Error("receipt lost")); await save;
  assert.match(h.el("basicSavedStatus").textContent, /回执未确认.*刷新列表/);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 0);
});

for (const endpoint of ["saved-list", "saved-select", "saved-save"])
  test(`late ${endpoint} response cannot restore a prior device's marks or metadata`, async () => {
    const h = harness({defaultView: true}); await h.load();
    const entry = savedEntry(h.plan.presets[0]);
    if (endpoint === "saved-select") { h.saved = [entry]; await h.click("basicSavedRefresh"); }
    if (endpoint === "saved-save") {
      h.responder = c => previewGesture(h, c); await h.draw([640, 360]);
      h.el("basicSavedLabel").value = entry.label;
    }
    const pending = deferred();
    h.responder = c => c.url.pathname === "/api/gestures/saved" ? pending.promise : undefined;
    const operation = endpoint === "saved-list" ? h.click("basicSavedRefresh") :
      endpoint === "saved-select" ? h.change("basicSaved", entry.gesture_id) : h.click("basicSave");
    h.run('selectionGeneration++; selectedDevice = "arm-02"; S.contextId = "ctx-2"; invalidateBasicMarks()');
    pending.resolve(endpoint === "saved-save" ? entry :
      {deviceId: "arm-01", contextId: "ctx-1", gestures: [entry]});
    await operation;
    assert.equal(h.run("BASIC.calculation"), null);
    assert.equal(h.run("BASIC.savedEntry"), null);
    assert.deepEqual(h.value("BASIC.saved"), []);
    assert.equal(h.el("basicSavedLabel").value, "");
    assert.equal(h.el("basicSavedStatus").textContent, "尚未读取已保存手势");
  });

for (const [label, change] of [
  ["request", h => h.run('BASIC.state.requestId = "ticket-2"')],
  ["interval", h => { h.el("basicInterval").value = "280"; }],
  ["point", h => h.run("BASIC.calculation.item.point.x = .7")],
  ["binding", h => { h.plan.binding.binding_revision++; }],
  ["profile", h => { h.plan.profile.tapDepth++; }],
  ["action gate", h => { h.plan.actions.double_tap.usable = false; }],
  ["unknown", h => h.run('BASIC.uncertain = {requestId:"other",session:"session-1"}')],
]) test(`manual preflight rejects changed ${label}, with no motion or manufactured unknown`, async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => previewGesture(h, c); await h.draw([256, 432]);
  const fresh = deferred(), count = h.calls.length;
  h.responder = c => c.url.pathname === "/api/gestures/plan" ? fresh.promise : undefined;
  const run = h.click("basicTest");
  await change(h); fresh.resolve(clone(h.plan)); await run;
  assert.deepEqual(h.calls.slice(count).map(c => c.url.pathname), ["/api/gestures/plan"]);
  assert.equal(h.storage.size, 0);
  if (label !== "unknown") assert.equal(h.run("BASIC.uncertain"), null);
  assert.match(h.el("basicStatus").textContent, /动作未提交/);
});

test("mobile pointer coordinates use the image rectangle and tolerate a small tap movement", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.el("camimg").getBoundingClientRect = () => ({left: 20, top: 100, width: 320, height: 180});
  h.responder = c => previewGesture(h, c);
  await h.draw([100, 208], [102, 210]);
  assert.deepEqual(h.value("basicPayload().point"), normalized(.25, .6));
  assert.equal(h.el("basicTest").disabled, false);
  await h.manual("swipe");
  await h.draw([20, 270], [260, 145]);
  const preview = h.calls.filter(c => c.url.pathname === "/api/gestures/preview").at(-1);
  assert.deepEqual(preview.body.start, cameraPoint(0, 680));
  assert.deepEqual(preview.body.end, cameraPoint(960, 180));
});

test("manual refresh failure never submits, retries or creates unknown", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => previewGesture(h, c); await h.draw([256, 432]);
  const count = h.calls.length;
  h.responder = c => c.url.pathname === "/api/gestures/plan" ? {httpError: 503} : undefined;
  await h.click("basicTest");
  assert.deepEqual(h.calls.slice(count).map(c => [c.method, c.url.pathname]),
    [["GET", "/api/gestures/plan"]]);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.match(h.el("basicStatus").textContent, /动作未提交/);
});

test("manual preflight locks duplicate clicks; unknown still forbids executing a new drawn point", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.responder = c => previewGesture(h, c); await h.draw([256, 432]);
  acceptGesture(h, "unknown");
  const accept = h.responder, pending = deferred();
  let reads = 0;
  h.responder = c => c.url.pathname === "/api/gestures/plan" && ++reads === 1 ?
    pending.promise : accept(c);
  const first = h.click("basicTest");
  await h.click("basicTest"); await h.click("basicTest");
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 0);
  pending.resolve(plan({frameSeq: 501})); await first;
  const original = h.value("BASIC.state.lastResult"), storage = [...h.storage];
  h.responder = c => previewGesture(h, c);
  await h.draw([384, 144]); await h.click("basicTest");
  assert.equal(h.calls.filter(c => c.url.pathname === "/api/gestures/test").length, 1);
  assert.deepEqual(h.value("BASIC.state.lastResult"), original);
  assert.deepEqual([...h.storage], storage);
  assert.equal(h.el("basicTest").disabled, true);
  assert.match(h.el("basicTestReason").textContent, /未核实|未知/);
});

test("ticket change during a named-plan read cannot restore stale authorization", async () => {
  const h = harness({defaultView: true}); await h.load();
  h.saved = [savedEntry(h.plan.presets[0])];
  await h.click("basicSavedRefresh");
  const pending = deferred();
  h.responder = c => c.url.pathname === "/api/gestures/saved" ? pending.promise : undefined;
  const selected = h.change("basicSaved", "gesture-1");
  h.state.requestId = "ticket-2"; await h.click("basicQuery");
  pending.resolve({deviceId: "arm-01", contextId: "ctx-1", gestures: h.saved});
  await selected;
  assert.equal(h.run("BASIC.calculation"), null);
  assert.equal(h.run("BASIC.savedEntry"), null);
  assert.deepEqual(h.value("BASIC.marks"), {});
  assert.equal(h.el("basicTest").disabled, true);
});

test("an explicit named-plan read works while unknown remains motion-blocked", async () => {
  const h = harness({defaultView: true});
  h.state = state({blocked: true, requestId: null, blockingReasons: ["原结果未知"],
    lastResult: {requestId: "unknown-1", outcome: "unknown", message: "原结果未知"}});
  await h.load();
  h.saved = [savedEntry(h.plan.presets[0])];
  await h.click("basicSavedRefresh"); await h.change("basicSaved", "gesture-1");
  assert.equal(h.run("BASIC.calculation.item.calculated"), true);
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.el("basicSave").disabled, false);
  assert.equal(h.run("BASIC.state.lastResult.outcome"), "unknown");
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("saved and swapped swipe remains enabled at the deadline without automatic requests", async () => {
  const h = harness({defaultView: true});
  h.state.expiresInMs = 120000;
  h.state.lastResult = {requestId: "prior-ticket", outcome: "confirmed", message: "上次动作已确认",
    evidence: {motionConfirmed: true, before: true}};
  await h.load();
  h.saved = [savedEntry(h.plan.presets[1], {label: "返回桌面"})];
  await h.click("basicSavedRefresh"); await h.change("basicSaved", "gesture-1");
  h.responder = c => previewGesture(h, c);
  await h.click("basicSwap"); await h.flush();
  assert.equal(h.el("basicTest").disabled, false);
  const count = h.calls.length, result = h.value("BASIC.state.lastResult");
  const calculation = h.value("BASIC.calculation"), marks = h.value("BASIC.marks");
  const status = h.el("basicStatus").textContent, evidence = [...h.el("basicEvidence").children];
  const expires = h.run("BASIC.expires");
  h.advance(119999);
  assert.equal(h.el("basicTest").disabled, false);
  h.advance(1);
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.el("basicTest").title, "执行滑动");
  assert.equal(h.el("basicTestReason").textContent, "");
  assert.doesNotMatch(h.el("basicCalculationSummary").textContent, /资格已过期/);
  assert.equal(h.el("basicQuery").disabled, false);
  assert.equal(h.el("basicSave").disabled, false);
  assert.equal(h.calls.length, count);
  assert.equal(h.run("BASIC.expires"), expires);
  assert.equal(h.run("BASIC.state.requestId"), "ticket-1");
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.deepEqual(h.value("BASIC.calculation"), calculation);
  assert.deepEqual(h.value("BASIC.marks"), marks);
  assert.equal(h.el("basicStatus").textContent, status);
  assert.deepEqual(h.el("basicEvidence").children, evidence);
  assert.equal(h.timers.size, 0);
});

test("a click before a delayed expiry timer fires refreshes the ticket and submits only once", async () => {
  const h = harness(); h.state.expiresInMs = 120000;
  h.state.lastResult = {requestId: "prior-ticket", outcome: "confirmed", message: "上次动作已确认"};
  await h.load(); await h.direction("up");
  const original = h.value("basicPayload()"), count = h.calls.length;
  acceptGesture(h);
  const accept = h.responder;
  let submitted = false;
  h.responder = call => {
    if (call.url.pathname === "/api/gestures/state" && !submitted)
      return state({requestId: "renewed-ticket"});
    if (call.url.pathname === "/api/gestures/test") submitted = true;
    return accept(call);
  };
  h.now += 120000;
  assert.equal(h.el("basicTest").disabled, false);
  await h.click("basicTest");
  const writes = h.calls.slice(count).filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body, {...original, requestId: "renewed-ticket"});
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  const completedCalls = h.calls.length, status = h.el("basicStatus").textContent;
  h.advance(1);
  assert.equal(h.calls.length, completedCalls);
  assert.equal(h.el("basicStatus").textContent, status);
});

test("explicit ticket query replaces the expiry timer; an old callback cannot affect the renewed ticket", async () => {
  const h = harness(); h.state.expiresInMs = 120000;
  await h.load(); await h.direction("up");
  assert.equal(h.timers.size, 1);
  const oldTimer = [...h.timers.values()][0];
  h.advance(60000);
  h.state.requestId = "ticket-2";
  await h.click("basicQuery");
  assert.equal(h.timers.size, 1);
  assert.equal(h.run("BASIC.state.requestId"), "ticket-2");
  const count = h.calls.length;
  h.advance(60000); oldTimer.callback();
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.timers.size, 1);
  h.advance(60000);
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.el("basicTestReason").textContent, "");
  assert.equal(h.calls.length, count);
  assert.equal(h.timers.size, 0);
});

test("device invalidation cancels the expiry timer without letting its callback repaint another device", async () => {
  const h = harness(); await h.load();
  assert.equal(h.timers.size, 1);
  const oldTimer = [...h.timers.values()][0];
  h.run('selectionGeneration++; selectedDevice = "arm-02"; S.contextId = "ctx-2"; invalidateBasicMarks()');
  assert.equal(h.timers.size, 0);
  const reason = h.el("basicTestReason").textContent, count = h.calls.length;
  oldTimer.callback(); h.advance(120000);
  assert.equal(h.el("basicTestReason").textContent, reason);
  assert.equal(h.calls.length, count);
  assert.equal(h.run("BASIC.state"), null);
});

test("failed state query cancels a previously armed deadline without renewing or executing", async () => {
  const h = harness(); await h.load();
  assert.equal(h.timers.size, 1);
  h.responder = c => c.url.pathname === "/api/gestures/state" ? {httpError: 503} : undefined;
  await h.click("basicQuery");
  assert.equal(h.timers.size, 0);
  const count = h.calls.length;
  h.advance(120000);
  assert.equal(h.calls.length, count);
  assert.equal(h.run("BASIC.state"), null);
  assert.equal(h.el("basicTest").disabled, true);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("deadline rendering preserves pending unknown evidence and never queries or clears the result", async () => {
  const h = harness();
  h.state.lastResult = {requestId: "unknown-1", outcome: "unknown", message: "原动作结果未知",
    evidence: {motionConfirmed: false, before: true}};
  h.storage.set("basic-gesture-pending:arm-01", JSON.stringify({requestId: "unknown-1", session: "session-1"}));
  await h.load();
  const count = h.calls.length, stored = [...h.storage], result = h.value("BASIC.state.lastResult");
  const status = h.el("basicStatus").textContent, evidence = [...h.el("basicEvidence").children];
  h.advance(60000);
  assert.equal(h.el("basicDoubleTap").disabled, true);
  assert.match(h.el("basicDoubleTapReason").textContent, /原请求结果未核实/);
  assert.equal(h.el("basicReview").hidden, false);
  assert.deepEqual([...h.storage], stored);
  assert.deepEqual(h.value("BASIC.state.lastResult"), result);
  assert.equal(h.el("basicStatus").textContent, status);
  assert.deepEqual(h.el("basicEvidence").children, evidence);
  assert.equal(h.calls.length, count);
});

test("saved options keep short labels and versions while retaining the exact reusable ID in details", async () => {
  const h = harness({defaultView: true}); await h.load();
  const id = "gesture-0123456789abcdef0123456789abcdef";
  h.saved = [savedEntry(h.plan.presets[0], {id, label: "返回桌面", version: 7}),
    {...savedEntry(h.plan.presets[1], {id: "unavailable-gesture", label: "返回上一级", version: 2}),
      usable: false}];
  await h.click("basicSavedRefresh");
  const options = h.el("basicSaved").options;
  assert.equal(options[1].textContent, "返回桌面 · v7");
  assert.equal(options[1].value, id);
  assert.ok(options[1].title.includes(id));
  assert.equal(options[2].textContent, "返回上一级 · v2 · 不可用");
  assert.ok(!options[1].textContent.includes(id));
  await h.change("basicSaved", id);
  assert.ok(h.el("basicSavedStatus").textContent.includes("编号：" + id));
  assert.match(h.el("basicSavedStatus").textContent, /版本：7/);
  assert.equal(h.calls.filter(c => c.method === "POST").length, 0);
});

test("actual API parser accepts a healthy camera response with error:null", async () => {
  const h = harness();
  const camera = {has_frame: true, seq: 5497991, width: 1280, height: 720, error: null};
  h.responder = c => c.url.pathname === "/api/camera/status" ? camera : undefined;
  const response = await h.run('_api("/api/camera/status")');
  assert.deepEqual(clone(response), camera);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].method, "GET");
  assert.equal(h.calls[0].url.searchParams.get("deviceId"), "arm-01");
  assert.equal(h.calls[0].url.searchParams.get("contextId"), "ctx-1");
});

test("real camera poll with error:null advances zero seq and a single point click previews without motion", async () => {
  const h = harness({defaultView: true});
  const camera = {has_frame: true, seq: 5497991, width: 1280, height: 720, error: null};
  h.plan.frameSeq = camera.seq;
  await h.load();
  h.run('S.camSeq = 0; const camImg = $("#camimg");');
  h.run(section("async function pollCamStatus()", "function redrawCamera()"));
  h.responder = c => c.url.pathname === "/api/camera/status" ? camera : previewGesture(h, c);
  assert.equal(h.run("S.camSeq"), 0);
  const count = h.calls.length;
  const poll = h.run("pollCamStatus()");
  await h.flush(); h.advance(400); await h.flush(); await poll;
  assert.equal(h.run("S.camSeq"), camera.seq);
  assert.equal(h.el("bCam").textContent, "实时画面 #" + camera.seq);
  assert.equal(h.calls.length, count + 1, "a healthy response must not cause a retry or context recovery");
  await h.draw([256, 432]);
  const writes = h.calls.filter(c => c.method === "POST");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url.pathname, "/api/gestures/preview");
  assert.deepEqual(writes[0].body, {deviceId: "arm-01", contextId: "ctx-1",
    action: "double_tap", point: cameraPoint(256, 432)});
  assert.equal(h.run("S.lastMark.frameSeq"), camera.seq);
  assert.deepEqual(h.value("basicPayload().point"), normalized(.2, .6));
  assert.equal(h.el("basicTest").disabled, false);
  assert.equal(h.el("basicDoubleTapCommand").hidden, true);
  assert.equal(h.run("BASIC.uncertain"), null);
  assert.equal(h.storage.size, 0);
  assert.equal(h.messages.some(message => /画面或尺寸未确认/.test(message.message)), false);
});

for (const error of ["相机不可用", {message: "相机读取失败", code: "camera_unavailable"}, {code: "camera_error"}])
  test(`actual API parser still rejects camera errors: ${JSON.stringify(error)}`, async () => {
    const h = harness();
    h.responder = c => c.url.pathname === "/api/camera/status" ? {error} : undefined;
    await assert.rejects(h.run('_api("/api/camera/status")'), failure => {
      assert.equal(failure.message, typeof error === "string" ? error : error.message || error.code);
      if (typeof error === "object") assert.equal(failure.code, error.code);
      return true;
    });
    assert.equal(h.calls.length, 1);
  });

test("actual context creation parser accepts error:null without changing the device or touching motion", async () => {
  const h = harness();
  const response = {deviceId: "arm-01", contextId: "ctx-new", ...h.value("S.context"), checks: [], error: null};
  h.run('DEVICE_ITEMS.set("arm-01", {deviceId:"arm-01"}); function renderDeviceContext() {}');
  h.run(section("async function createDeviceContext()", "let contextRecovery"));
  h.responder = c => c.url.pathname === "/api/device-context" ? response : undefined;
  const actual = await h.run("createDeviceContext()");
  assert.deepEqual(clone(actual), response);
  assert.equal(h.run("S.contextId"), "ctx-new");
  assert.equal(h.run("DEV_RAW()"), "arm-01");
  assert.deepEqual(h.calls.map(c => [c.method, c.url.pathname]), [["POST", "/api/device-context"]]);
  assert.equal(h.run("BASIC.uncertain"), null);
});

test("actual context creation parser still rejects a structured error before replacing context", async () => {
  const h = harness();
  h.run('DEVICE_ITEMS.set("arm-01", {deviceId:"arm-01"}); function renderDeviceContext() {}');
  h.run(section("async function createDeviceContext()", "let contextRecovery"));
  h.responder = c => c.url.pathname === "/api/device-context" ?
    {error: {message: "当前绑定未确认", code: "binding_unconfirmed"}} : undefined;
  await assert.rejects(h.run("createDeviceContext()"), /当前绑定未确认/);
  assert.equal(h.run("S.contextId"), "ctx-1");
  assert.equal(h.calls.length, 1);
});

test("fixture loads the real debug session and both write-header paths", async () => {
  const h = harness();
  h.responder = c => c.url.pathname === "/api/debug-session"
    ? {csrf_token: "refreshed-gesture-csrf"} : {accepted: true};
  await h.run("loadDebugSession()");
  assert.deepEqual(h.value("DEBUG_WRITE_HEADERS()"), {
    "Content-Type": "application/json", "X-Debug-CSRF": "refreshed-gesture-csrf",
  });
  await h.run('_api("/api/gestures/preview", {method: "POST", ' +
    'headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "double_tap"})})');
  assert.deepEqual(h.calls.map(c => [c.method, c.url.pathname]), [
    ["GET", "/api/debug-session"], ["POST", "/api/gestures/preview"],
  ]);
  assert.equal(h.calls[1].headers["X-Debug-CSRF"], "refreshed-gesture-csrf");
  assert.equal(h.calls[1].body.contextId, "ctx-1");
});

test("fixture runs real ROI invalidation on a camera binding error", async () => {
  const h = harness();
  h.run(`REGION.frame = {evidenceId: "old-frame"}; REGION.rect = {x: 0, y: 0, width: 1, height: 1};
    REGION.preview = {preview_id: "old-preview"}; REGION.ready = true; REGION.framing = true;`);
  h.responder = () => ({error: {code: "camera_binding_revision_changed", message: "相机绑定已变化"}});
  await assert.rejects(h.run('_api("/api/camera/status")'),
    error => error.code === "camera_binding_revision_changed");
  assert.deepEqual(h.value("[REGION.frame, REGION.rect, REGION.preview]"), [null, null, null]);
  assert.equal(h.run("REGION.ready"), false);
  assert.equal(h.run("REGION.framing"), false);
  assert.match(h.el("regionMsg").textContent, /上下文已失效/);
  assert.equal(h.el("btnRegionSave").disabled, true);
  assert.equal(h.el("btnRegionDraft").disabled, true);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].method, "GET");
});
