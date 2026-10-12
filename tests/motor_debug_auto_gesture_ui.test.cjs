// 纯 UI 测试：用 fake DOM + fake fetch 执行页面真实的自动手势区代码（§13）。
// 不导入后端、不连设备：按钮状态唯一来源是后端资格回包；动作走
// /api/auto-navigate 单步导航代理；双击走旧 screen_gesture 通道（§22.17）。
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
  const last = end === undefined ? script.length : script.indexOf(end, first + start.length);
  assert.ok(last > first, `Missing page section end: ${end}`);
  return script.slice(first, last);
};
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

const sha = character => character.repeat(64);
function binding() {
  return {device_id: "arm-01", daemon_session: "session-1", camera_id: "cam-01",
    camera_device_unique_id: "uvc-01", binding_revision: 7,
    calibration_fingerprint: sha("a"), frame_width: 1280, frame_height: 720,
    coordinate_frame_hash: sha("b"), gesture_profile_version: 2,
    recipe_version: "gesture-rules.v1"};
}
function gestureEntry(usable = true, reasons = []) {
  return {gesture_id: "double_tap", version: 1, state: usable ? "valid" : "unverified",
    state_display: usable ? "有效" : "未验证", calculated: usable,
    controller_supported: usable, contact_validated: usable, effect_validated: usable,
    usable, blocking_reasons: reasons,
    validation_status: usable ? "passed" : "not_tested"};
}
function navigationAction(usable = true, reasons = []) {
  return {usable, blocking_reasons: reasons};
}
const unknownStep = {
  action: "automatic_navigation", controller_status: "unknown",
  contact_status: "unknown", effect_status: "unknown", top_verified: false,
  reason_code: "effect_unknown", message: "手机效果无法确认",
};

function harness() {
  const nodes = new Map();
  class Element {
    constructor(tag = "div", id = "") {
      this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.events = {};
      this.textContent = ""; this._value = ""; this._checked = false;
      this.disabled = false; this.hidden = false; this.dataset = {};
      this.style = {};
      this.classList = {toggle() {}};
    }
    get value() { return this._value; }
    set value(value) { this._value = value; }
    get checked() { return this._checked; }
    set checked(value) { this._checked = value; }
    addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
    getContext(type) {
      assert.equal(this.tagName, "CANVAS");
      assert.equal(type, "2d");
      return {clearRect() {}, fillRect() {}, fillText() {}, strokeRect() {}, drawImage() {}};
    }
    async dispatch(name, event = {}) {
      for (const callback of this.events[name] || [])
        await callback({target: this, preventDefault() {}, stopPropagation() {}, ...event});
    }
  }
  for (const match of html.matchAll(/<([a-z]+)\b[^>]*>/g)) {
    const id = match[0].match(/\bid="([^"]+)"/)?.[1];
    if (!id) continue;
    const element = new Element(match[1], id);
    // 初始 disabled/hidden 以页面标记为准：资格未读到时按钮必须保持置灰
    element.disabled = /\sdisabled\b/.test(match[0]);
    element.hidden = /\shidden\b/.test(match[0]);
    if (element.tagName === "CANVAS") {
      element.width = Number(match[0].match(/\bwidth="(\d+)"/)?.[1] || 300);
      element.height = Number(match[0].match(/\bheight="(\d+)"/)?.[1] || 150);
    }
    nodes.set(id, element);
  }
  const query = selector => {
    if (!selector.startsWith("#")) throw new Error(`Unsupported selector: ${selector}`);
    assert.ok(nodes.has(selector.slice(1)), `Missing HTML element: ${selector}`);
    return nodes.get(selector.slice(1));
  };
  const storage = new Map(), calls = [], messages = [], timers = new Map();
  let timerId = 0;
  const h = {nodes, storage, calls, messages, timers, responder: null, now: Date.now(),
    basicGestureCalls: [],
    gestures: {contract_version: "automatic-gesture-profile.v1", device_id: "arm-01",
      binding: binding(), blocking_reasons: [],
      actions: {double_tap: gestureEntry(true), scroll_up: gestureEntry(true),
        scroll_down: gestureEntry(true), system_back_left: gestureEntry(true),
        system_back_right: gestureEntry(true), system_home: gestureEntry(true)}},
    navigation: {contract_version: "automatic-gesture-profile.v1", device_id: "arm-01",
      binding: binding(), blocking_reasons: [], profile_id: "wx_chat_list_auto",
      version: 3, owner_plugin: "wx-wechat-adapter", layout_version: "wechat-list-layout.v1",
      actions: {search_next: navigationAction(true), search_previous: navigationAction(true),
        return_top: navigationAction(true), back_to_list: navigationAction(true),
        home: navigationAction(true)},
      navigation_limits: {max_steps: 8, max_physical_actions: 8, timeout_ms: 90000,
        max_boundary_probes: 2}},
    settings: {device_id: "arm-01", automatic_gestures_enabled: true,
      automatic_navigation_enabled: true,
      gesture_program_enabled: false, gesture_rules_version: "gesture-rules.v1",
      calibrationReady: true},
    task: {task_id: "task-1", status: "confirmed", status_display: "已发送或已完成",
      result_message: "本次上滑起效，列表内容位移已验证",
      result: {steps: [{action: "automatic_navigation", controller_status: "completed",
        contact_status: "not_observed", effect_status: "verified", top_verified: false,
        reason_code: null, message: "本次上滑起效，列表内容位移已验证"}]},
      evidence: [{evidence_id: "ev-before-1"}, {evidence_id: "ev-after-1"}]}};
  const defaultResponse = call => {
    const p = call.url.pathname;
    if (call.method === "GET") {
      if (p === "/api/auto-gestures") return clone(h.gestures);
      if (p === "/api/auto-navigation") return clone(h.navigation);
      if (p === "/api/auto-gestures/settings") return clone(h.settings);
      if (p === "/api/auto-navigate") return clone(h.task);
      throw new Error(`Unexpected GET: ${p}`);
    }
    if (call.method === "POST" && p === "/api/auto-navigate")
      return {task_id: h.task.task_id, status: "queued", status_display: "已排队",
        created_at: "2026-09-08T00:00:00Z"};
    if (call.method === "POST" && p === "/api/navigation-runs/close")
      return {navigation_id: call.body.navigationId, state: "completed", reason_code: null};
    if (call.method === "POST" && p === "/api/auto-gestures/validate")
      return {task_id: "gval-1", status: "queued", status_display: "已排队",
        created_at: "2026-09-08T00:00:00Z"};
    if (call.method === "PUT" && p === "/api/auto-gestures/settings") {
      Object.assign(h.settings, clone(call.body.updates));
      return clone(h.settings);
    }
    throw new Error(`Unexpected write: ${call.method} ${p}`);
  };
  const context = vm.createContext({
    console, URL, URLSearchParams,
    Date: class extends Date { static now() { return h.now; } },
    setTimeout: (callback, delay) => {
      const id = ++timerId;
      timers.set(id, {callback, deadline: h.now + delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
    document: {querySelector: query, querySelectorAll: selector => [query(selector)],
      createElement: tag => new Element(tag)},
    window: Object.assign(new Element("window"), {location: {origin: "http://ui-test.invalid"}}),
    location: {origin: "http://ui-test.invalid"},
    sessionStorage: {getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key)},
    toast: (message, type) => messages.push({message, type}),
    recoverDeviceContext: () => { throw new Error("Unexpected context recovery"); },
    runBasicGesture: presetId => { h.basicGestureCalls.push(presetId); },
    fetch: async (url, options) => {
      const call = {url: new URL(url, "http://ui-test.invalid"),
        method: options.method || "GET",
        headers: {...options.headers}, body: options.body ? JSON.parse(options.body) : null};
      assert.equal(call.url.origin, "http://ui-test.invalid", "fixture requests stay isolated");
      if (["POST", "PUT", "DELETE"].includes(call.method)) {
        assert.ok(h.run("DEBUG_CSRF"), "a debug session must be established before writes");
        assert.equal(call.headers["X-Debug-CSRF"], h.run("DEBUG_CSRF"));
      }
      calls.push(call);
      let data;
      if (h.responder) data = await h.responder(call);
      if (data === undefined) data = defaultResponse(call);
      return {ok: !data.httpError, status: data.httpError || 200,
        json: async () => data.httpError
          ? {error: {message: data.message || `HTTP ${data.httpError}`}} : data};
    },
  });
  vm.runInContext(`
    const $ = s => document.querySelector(s), $$ = s => document.querySelectorAll(s);
    let deviceSelectionError = "";
    const DEV_RAW = () => selectedDevice, DEV = () => encodeURIComponent(selectedDevice);
    ${section("const S = {", "let selectedDevice")}
    S.contextId = "ctx-1";
    selectedDevice = "arm-01";
    ${section("let DEBUG_CSRF =", "async function api(")}
    // Session bootstrap is already complete for gesture cases; it adds no gesture request.
    DEBUG_CSRF = "isolated-debug-csrf";
    ${section("let contextRecovery =", "async function recoverDeviceContext()")}
    ${section("async function api(", "function setBusy(")}
    ${section("// ---- FIX-12 手机粗框（真实源预览）BEGIN ----",
      "// ---- FIX-12 手机粗框（真实源预览）END ----")}
    regionInvalidate(DEV_RAW(), S.contextId);
    ${section("// ---------- 自动手势区")}
  `, context);
  h.run = expression => vm.runInContext(expression, context);
  h.value = expression => clone(h.run(expression));
  h.el = id => nodes.get(id);
  h.click = id => h.el(id).dispatch("click");
  h.flush = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };
  h.advance = ms => {
    h.now += ms;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.deadline <= h.now)
        .sort((a, b) => a[1].deadline - b[1].deadline)[0];
      if (!next) break;
      timers.delete(next[0]); next[1].callback();
    }
  };
  h.refresh = () => h.run("refreshAutoGestures()");
  h.posts = pathname => calls.filter(c => c.method === "POST" && c.url.pathname === pathname);
  return h;
}

test("page script parses; the auto-gesture panel adds the required keys after #basicGesturePanel", () => {
  new vm.Script(script);
  // 开关已删（2026-09-30 拍板）：页面不再有 autoGSaveSettings/Setting* 控件
  for (const id of ["autoGesturePanel", "autoGTestBtn", "autoGQuery",
    "autoGTarget", "autoGStatus", "autoGResult",
    "autoGSearchNext",
    "autoGSearchPrevious", "autoGReturnTop", "autoGBackToList", "autoGHome",
    "autoGDoubleTap"])
    assert.ok(html.includes(`id="${id}"`), `Missing HTML element: ${id}`);
  for (const gone of ["autoGSaveSettings", "autoGSettingGestures",
    "autoGSettingNavigation", "autoGSettingAutoValidate"])
    assert.equal(html.includes(`id="${gone}"`), false, `${gone} should be removed`);
  assert.ok(html.indexOf('id="autoGesturePanel"') > html.indexOf('id="basicGesturePanel"'),
    "the panel must come after #basicGesturePanel");
  assert.ok(html.indexOf('id="ocrNx"') > html.indexOf('id="autoGesturePanel"'),
    "the old camera panel content must stay in place");
  // 旧区约束不回归：basicGesturePanel..ocrNx 之间只有既定数字输入
  // （basicInterval 50ms + basicStrokeMm 滑动行程，2026-09-30 新增后者）
  const legacy = html.slice(html.indexOf('id="basicGesturePanel"'), html.indexOf('id="ocrNx"'));
  assert.equal([...legacy.matchAll(/<input[^>]+type="number"/g)].length, 2);
  // bootstrap 与第 7 个 interval 挂上刷新；设备切换清理入口存在
  assert.match(section("async function bootstrap()", "if (document.readyState"), /refreshAutoGestures\(\);/);
  assert.equal([...script.matchAll(/setInterval\(/g)].length, 7);
  assert.match(script, /setInterval\(refreshAutoGestures, 10000\)/);
  const switcher = script.slice(script.indexOf('$("#deviceId").addEventListener'),
    script.indexOf("// ---------- 输入法设备清单"));
  assert.match(switcher, /resetAutoGestures\(\);/);
  assert.match(switcher, /refreshAutoGestures\(\);/);
});

test("initial refresh sends exactly the three read-only GETs and never a POST", async () => {
  const h = harness();
  await h.refresh();
  assert.deepEqual(h.calls.map(c => [c.method, c.url.pathname]), [
    ["GET", "/api/auto-gestures"],
    ["GET", "/api/auto-navigation"],
    ["GET", "/api/auto-gestures/settings"],
  ]);
  assert.equal(h.calls.some(c => c.method !== "GET"), false);
  for (const id of ["autoGSearchNext", "autoGSearchPrevious", "autoGReturnTop", "autoGHome"])
    assert.equal(h.el(id).disabled, false, `${id} usable by backend state`);
  assert.equal(h.el("autoGBackToList").disabled, true, "back_to_list needs a WX session");
  assert.equal(h.el("autoGDoubleTap").disabled, false);
  assert.equal(h.el("autoGTestBtn").disabled, false);
  assert.equal(h.el("autoGQuery").hidden, true);
  assert.equal(h.timers.size, 0, "no polling timers for read-only refresh");
});

test("开关已删除（2026-09-30 拍板）：无保存控件，校准未就绪只出提示且绝不发 PUT", async () => {
  const h = harness();
  h.settings.calibrationReady = false;
  await h.refresh();
  assert.match(h.el("autoGSettingGate").textContent, /自动校准未完成/);
  assert.equal(h.calls.some(c => c.method === "PUT"), false);
  h.settings.calibrationReady = true;
  await h.refresh();
  assert.equal(h.el("autoGSettingGate").textContent, "");
  // 页面任何路径都不再触发设置写入
  await h.click("autoGTestBtn"); await h.flush();
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(h.calls.some(c => c.method === "PUT"), false);
});

test("button state comes from the backend payload, not from local calibration color", async () => {
  const h = harness();
  h.navigation.actions.search_next = navigationAction(false,
    [{code: "automatic_navigation_disabled", message: "设备自动导航开关未开启"}]);
  h.gestures.actions.double_tap = gestureEntry(false,
    [{code: "profile_missing", message: "尚未建立自动档案"}]);
  await h.refresh();
  assert.equal(h.el("autoGSearchNext").disabled, true);
  assert.equal(h.el("autoGDoubleTap").disabled, true);
  assert.match(h.el("autoGStatus").textContent, /设备自动导航开关未开启/);
  assert.match(h.el("autoGStatus").textContent, /尚未建立自动档案/);
  assert.equal(h.el("autoGSearchPrevious").disabled, false);
  await h.click("autoGSearchNext");
  assert.equal(h.posts("/api/auto-navigate").length, 0);
  h.navigation.actions.search_next = navigationAction(true);
  h.gestures.actions.double_tap = gestureEntry(true);
  await h.refresh();
  assert.equal(h.el("autoGSearchNext").disabled, false);
  assert.equal(h.el("autoGDoubleTap").disabled, false);
});

test("one click on 向上找 submits exactly one navigate step with the eligibility binding", async () => {
  const h = harness();
  const submit = deferred();
  h.responder = call => {
    if (call.method === "POST" && call.url.pathname === "/api/auto-navigate")
      return submit.promise;
    return undefined;
  };
  await h.refresh();
  const click = h.click("autoGSearchPrevious");
  await h.flush();
  const writes = h.posts("/api/auto-navigate");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.action, "search_previous");
  assert.equal(writes[0].body.stepIndex, 1);
  assert.deepEqual(writes[0].body.binding, h.navigation.binding);
  assert.equal(writes[0].body.navigationProfileVersion, 3);
  // navigation_id 三道关卡（schema 正则 / uuid.UUID / wx 插件）都要求标准小写 UUID：
  // 带 debug- 前缀的编号会被全部拒绝（P0-3）
  assert.match(writes[0].body.navigationId,
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  assert.equal(writes[0].url.searchParams.get("deviceId"), null,
    "device identity travels in the POST body");
  // 请求期间禁用重复提交（data-busy + disabled）
  assert.equal(h.el("autoGSearchPrevious").disabled, true);
  assert.equal(h.el("autoGSearchPrevious").dataset.busy, "1");
  assert.equal(h.el("autoGSearchNext").disabled, true);
  await h.click("autoGSearchPrevious");
  await h.click("autoGSearchNext");
  assert.equal(h.posts("/api/auto-navigate").length, 1);
  assert.ok(h.storage.has("auto-gesture-pending:arm-01"), "pending record kept before receipt");
  submit.resolve({task_id: "task-9", status: "queued", status_display: "已排队"});
  await h.flush();
  await h.flush();
  await click;
  const polls = h.calls.filter(c => c.method === "GET" && c.url.pathname === "/api/auto-navigate");
  assert.equal(polls.length, 1);
  assert.equal(polls[0].url.searchParams.get("taskId"), "task-9");
  const closes = h.posts("/api/navigation-runs/close");
  assert.equal(closes.length, 1);
  assert.equal(closes[0].body.reason, "caller_finished");
  assert.equal(closes[0].body.navigationId, writes[0].body.navigationId);
  assert.equal(h.el("autoGSearchPrevious").disabled, false);
  assert.equal(h.el("autoGSearchPrevious").dataset.busy, undefined);
  assert.match(h.el("autoGResult").textContent, /指令执行：completed/);
  assert.match(h.el("autoGResult").textContent, /触摸情况：not_observed/);
  assert.match(h.el("autoGResult").textContent, /手机效果：verified/);
  assert.match(h.el("autoGResult").textContent, /列表内容位移已验证/);
  assert.match(h.el("autoGResult").textContent, /ev-before-1/);
  assert.equal(h.storage.has("auto-gesture-pending:arm-01"), false);
  assert.equal(h.el("autoGQuery").hidden, true);
});

test("search target input is forwarded only for search actions", async () => {
  const h = harness();
  await h.refresh();
  h.el("autoGTarget").value = "张三";
  await h.click("autoGSearchNext"); await h.flush();
  assert.deepEqual(h.posts("/api/auto-navigate")[0].body.target,
    {visible_name: "张三", external_conversation_id: "张三"});
  await h.click("autoGReturnTop"); await h.flush();
  const second = h.posts("/api/auto-navigate")[1];
  assert.equal(second.body.action, "return_top");
  assert.equal("target" in second.body, false);
});

test("server-rejected submission clears pending so the panel is not locked", async () => {
  const h = harness();
  let rejections = 0;
  h.responder = call => {
    if (call.method === "POST" && call.url.pathname === "/api/auto-navigate") {
      rejections += 1;
      return {httpError: 409, message: "navigation_step_conflict"};
    }
    return undefined;
  };
  await h.refresh();
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(rejections, 1);
  // 服务端明确拒绝 = 确定未提交：pending 必须清除，否则无 taskId 的残留
  // 会永久锁死面板（P1-14：查询原结果也因无任务号而不可用）
  assert.equal(h.storage.has("auto-gesture-pending:arm-01"), false,
    "definitely-not-submitted must clear the pending record");
  assert.equal(h.el("autoGQuery").hidden, true);
  h.responder = null;
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 2,
    "a rejected submission must not block the next one");
});

test("proxy 502 (upstream timeout) keeps pending: task may exist server-side", async () => {
  const h = harness();
  h.responder = call => {
    if (call.method === "POST" && call.url.pathname === "/api/auto-navigate")
      return {httpError: 502, message: "上游超时"};
    return undefined;
  };
  await h.refresh();
  await h.click("autoGSearchNext"); await h.flush();
  // 代理 502 = 上游 5s 超时，任务可能已建：确定未提交的判据只认 4xx（R2 N3）
  assert.ok(h.storage.has("auto-gesture-pending:arm-01"),
    "5xx rejection must keep pending (result unknown, never resend)");
  h.responder = null;
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 1,
    "unresolved pending must block resubmission");
});

test("network-lost submission keeps pending as unresolved (never resends)", async () => {
  const h = harness();
  h.responder = call => {
    if (call.method === "POST" && call.url.pathname === "/api/auto-navigate") {
      const error = new TypeError("fetch failed");
      throw error;
    }
    return undefined;
  };
  await h.refresh();
  await h.click("autoGSearchNext"); await h.flush();
  // 网络层失败 = 是否送达未知：保留 pending，禁止重发（宁锁死不重发）
  assert.ok(h.storage.has("auto-gesture-pending:arm-01"),
    "network-lost submission stays unresolved");
  h.responder = null;
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 1,
    "unresolved pending must block resubmission");
});

test("unknown terminal keeps the original request and only allows a read-only query", async () => {
  const h = harness();
  h.task = {task_id: "task-u", status: "unknown", status_display: "结果未知",
    result_message: "手机效果无法确认", result: {steps: [clone(unknownStep)]},
    evidence: [{evidence_id: "ev-unknown-1"}]};
  await h.refresh();
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 1);
  assert.equal(h.posts("/api/navigation-runs/close").length, 0,
    "unknown must not be auto-closed");
  assert.equal(h.el("autoGQuery").hidden, false);
  assert.equal(h.el("autoGSearchNext").disabled, false, "buttons restore after a full result");
  assert.match(h.el("autoGResult").textContent, /指令执行：unknown/);
  assert.match(h.el("autoGResult").textContent, /effect_unknown/);
  assert.ok(h.storage.has("auto-gesture-pending:arm-01"));
  await h.click("autoGQuery"); await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 1,
    "the query button must never re-submit the physical action");
  assert.equal(h.posts("/api/navigation-runs/close").length, 0);
  const query = h.calls.filter(c => c.method === "GET" && c.url.pathname === "/api/auto-navigate");
  assert.equal(query.length, 2);
  assert.equal(query[1].url.searchParams.get("taskId"), "task-u");
  assert.ok(h.storage.has("auto-gesture-pending:arm-01"), "unknown stays unresolved");
});

test("queued steps poll on a 2s cadence until a terminal state, then close once", async () => {
  const h = harness();
  h.task = {task_id: "task-2", status: "queued", status_display: "已排队",
    result: {steps: []}, evidence: []};
  h.responder = call => {
    if (call.method === "GET" && call.url.pathname === "/api/auto-navigate") {
      if (call.url.searchParams.get("taskId") === "task-2") return clone(h.task);
      return undefined;
    }
    return undefined;
  };
  await h.refresh();
  const click = h.click("autoGHome");
  await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 1);
  assert.equal(h.el("autoGHome").disabled, true);
  h.task.status = "executing";
  h.advance(2000); await h.flush();
  h.task.status = "confirmed";
  h.task.status_display = "已发送或已完成";
  h.task.result = {steps: [{action: "automatic_navigation", controller_status: "completed",
    contact_status: "not_observed", effect_status: "verified", top_verified: true,
    reason_code: null, message: "已回到桌面"}]};
  h.advance(2000); await h.flush();
  await click;
  assert.equal(h.el("autoGHome").disabled, false);
  assert.equal(h.posts("/api/navigation-runs/close").length, 1);
  assert.equal(h.posts("/api/navigation-runs/close")[0].body.reason, "caller_finished");
  assert.match(h.el("autoGResult").textContent, /已回到桌面/);
});

test("the double-tap button reuses the legacy screen_gesture path, never the proxy", async () => {
  const h = harness();
  await h.refresh();
  await h.click("autoGDoubleTap"); await h.flush();
  assert.deepEqual(h.basicGestureCalls, ["double_tap_center"]);
  assert.equal(h.calls.some(c => c.url.pathname === "/api/auto-navigate"), false);
  assert.equal(h.posts("/api/auto-gestures/validate").length, 0);
});

test("测试手势 submits one validate task without operator fields", async () => {
  const h = harness();
  await h.refresh();
  await h.click("autoGTestBtn"); await h.flush();
  const writes = h.posts("/api/auto-gestures/validate");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.deviceId, "arm-01");
  assert.equal("operator_id" in writes[0].body, false);
  assert.equal("reason" in writes[0].body, false);
  assert.match(h.el("autoGResult").textContent, /自检任务已提交/);
});

test("upstream 502 keeps every action disabled and never fabricates usability", async () => {
  const h = harness();
  h.responder = call => {
    if (call.method === "GET" && call.url.pathname === "/api/auto-navigation")
      return {httpError: 502, message: "上游服务不可达（http://127.0.0.1:8788）"};
    return undefined;
  };
  await h.refresh();
  for (const id of ["autoGSearchNext", "autoGSearchPrevious", "autoGReturnTop", "autoGHome"])
    assert.equal(h.el(id).disabled, true);
  assert.equal(h.el("autoGDoubleTap").disabled, false, "gestures view still loads");
  assert.match(h.el("autoGStatus").textContent, /上游服务不可达/);
  await h.click("autoGSearchNext");
  assert.equal(h.posts("/api/auto-navigate").length, 0);
});

test("switching devices clears the old preview but keeps the latest result text", async () => {
  const h = harness();
  h.task = {task_id: "task-u", status: "unknown", status_display: "结果未知",
    result: {steps: [clone(unknownStep)]}, evidence: []};
  await h.refresh();
  await h.click("autoGSearchNext"); await h.flush();
  assert.match(h.el("autoGResult").textContent, /指令执行：unknown/);
  h.run('selectionGeneration++; selectedDevice = "arm-02"; resetAutoGestures()');
  for (const id of ["autoGSearchNext", "autoGSearchPrevious", "autoGReturnTop",
    "autoGHome", "autoGDoubleTap", "autoGTestBtn"])
    assert.equal(h.el(id).disabled, true, `${id} cleared with the old preview`);
  assert.match(h.el("autoGStatus").textContent, /尚未读取/);
  // 不清未知结果：结果文本保留，旧设备的 pending 记录保留
  assert.match(h.el("autoGResult").textContent, /指令执行：unknown/);
  assert.ok(h.storage.has("auto-gesture-pending:arm-01"));
  assert.equal(h.el("autoGQuery").hidden, true, "new device has no pending record");
  await h.refresh();
  assert.equal(h.el("autoGSearchNext").disabled, false);
  assert.match(h.el("autoGResult").textContent, /指令执行：unknown/);
});

test("late eligibility responses after a device switch are discarded", async () => {
  const h = harness();
  const pending = deferred();
  let count = 0;
  h.responder = call => {
    if (call.method === "GET" && call.url.pathname === "/api/auto-gestures"
        && ++count === 1) return pending.promise;
    return undefined;
  };
  const refresh = h.refresh();
  h.run('selectionGeneration++; selectedDevice = "arm-02"');
  pending.resolve(clone(h.gestures));
  await refresh; await h.flush();
  assert.equal(h.run("AUTO_G.gestures"), null);
  assert.equal(h.el("autoGSearchNext").disabled, true);
});

test("a submit whose receipt never arrives stays query-only and is never replayed", async () => {
  const h = harness();
  const submit = deferred();
  h.responder = call => {
    if (call.method === "POST" && call.url.pathname === "/api/auto-navigate")
      return submit.promise;
    return undefined;
  };
  await h.refresh();
  const click = h.click("autoGSearchNext");
  await h.flush();
  submit.reject(new Error("simulated lost response"));
  await h.flush(); await click;
  assert.equal(h.posts("/api/auto-navigate").length, 1);
  assert.equal(h.posts("/api/navigation-runs/close").length, 0);
  assert.equal(h.el("autoGSearchNext").disabled, false);
  assert.match(h.el("autoGResult").textContent, /未确认/);
  await h.click("autoGSearchNext"); await h.flush();
  assert.equal(h.posts("/api/auto-navigate").length, 1,
    "no second physical submission while the original is unresolved");
});

test("fixture loads the real debug session and automatic writes keep its CSRF header", async () => {
  const h = harness();
  h.responder = c => c.url.pathname === "/api/debug-session"
    ? {csrf_token: "refreshed-auto-gesture-csrf"} : undefined;
  await h.run("loadDebugSession()");
  assert.deepEqual(h.value("DEBUG_WRITE_HEADERS()"), {
    "Content-Type": "application/json", "X-Debug-CSRF": "refreshed-auto-gesture-csrf",
  });
  await h.refresh();
  await h.click("autoGTestBtn"); await h.flush();
  const writes = h.posts("/api/auto-gestures/validate");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].headers["X-Debug-CSRF"], "refreshed-auto-gesture-csrf");
  assert.equal(writes[0].body.contextId, "ctx-1");
  assert.equal(h.posts("/api/auto-navigate").length, 0);
});

test("fixture runs real ROI invalidation without emitting automatic gestures", async () => {
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
