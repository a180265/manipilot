// FIX-12（2026-10-04 实施单）：调试台粗框接真实源预览 + A→B→A 设备切换隔离。
// F 编号映射：
// - F12-1 → test_f12_1_region_wired_to_real_three_routes_and_zero_motion
// - F12-2 → test_f12_2_letterbox_transform_roundtrip_and_edge_rounding
// - F12-3 → test_f12_3_abab_late_responses_cannot_clobber_current_context
//           test_f12_3_stale_frame_context_closes_only_itself_and_blocks_actions
// - F12-4 → test_f12_4_framing_without_calibration_and_honest_version_display
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const html = fs.readFileSync(
  path.join(__dirname, "../scripts/motor_debug_page.html"), "utf8");
const server = fs.readFileSync(
  path.join(__dirname, "../scripts/motor_debug_server.py"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const regionCode = script.slice(
  script.indexOf("// ---- FIX-12 手机粗框（真实源预览）BEGIN ----"),
  script.indexOf("// ---- FIX-12 手机粗框（真实源预览）END ----"));
assert.ok(regionCode.includes("var REGION"), "region module must be present");
new vm.Script(script);  // 全页脚本语法自检（含粗框模块）

function makeElement() {
  const listeners = {};
  return {
    listeners,
    style: {}, className: "", textContent: "", innerHTML: "",
    disabled: false, width: 640, height: 360,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    getBoundingClientRect() { return {left: 0, top: 0, width: 640, height: 360}; },
    getContext() {
      return new Proxy({}, {get: (t, k) => (k in t ? t[k] : () => {})});
    },
  };
}

function harness({devices = ["arm-01", "arm-02"]} = {}) {
  const elements = {};
  const calls = [];       // {path, opts}
  const pending = [];     // 未决请求（FIFO）
  const toasts = [];
  let currentDevice = "arm-01";
  const context = {
    console, Math, JSON, Date, Number, String, Boolean, Object, Array, Map,
    Promise, isNaN, parseInt, encodeURIComponent, Proxy,
    deviceSelectionError: "",
    DEVICE_ITEMS: new Map(devices.map(d => [d, {deviceId: d}])),
    S: {contextId: "ctx-1"},
    DEV_RAW: () => currentDevice,
    setDevice(d) { currentDevice = d; },
    api: (p, opts) => new Promise((resolve, reject) => {
      calls.push({path: p, opts});
      pending.push({path: p, resolve, reject});
    }),
    toast: (...a) => toasts.push(a),
    Image: class {
      constructor() { this.naturalWidth = 640; this.naturalHeight = 480; }
      set src(v) { this._src = v; }
      get src() { return this._src; }
      decode() { return Promise.resolve(); }
    },
    document: {readyState: "complete"},
    window: {addEventListener() {}},
    $: sel => elements[sel] || (elements[sel] = makeElement()),
  };
  vm.runInNewContext(regionCode, context);
  return {
    context, calls, pending, toasts, elements,
    resolve(pathSuffix, payload) {
      // R2-15：region GET 现附 contextId（不以 device 参数结尾）→ 用 includes
      const item = pending.splice(pending.findIndex(
        p => p.path.includes(pathSuffix)), 1)[0];
      assert.ok(item, `no pending request for ${pathSuffix}`);
      item.resolve(payload);
    },
    reject(pathSuffix, error) {
      const item = pending.splice(pending.findIndex(
        p => p.path.includes(pathSuffix)), 1)[0];
      assert.ok(item, `no pending request for ${pathSuffix}`);
      item.reject(error);
    },
    pendingFor(pathSuffix) {
      return pending.filter(p => p.path.includes(pathSuffix)).length;
    },
  };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

// ---------------- F12-1：三个正式路由接线、顺序固定、零动作 ----------------
test("f12_1_region_wired_to_real_three_routes_and_zero_motion", () => {
  // 页面固定顺序：设备绑定 → 粗框 → 统一校准向导 → 允许输入与发送
  const idxRegion = html.indexOf('id="regionEditor"');
  const idxWizard = html.indexOf("🧭 统一校准向导");
  const idxInput = html.indexOf("允许输入与发送（A-开关");
  assert.ok(idxRegion > 0 && idxWizard > idxRegion && idxInput > idxWizard,
    "入口顺序必须是 绑定→粗框→自动校准→允许输入与发送");
  // 服务端代理接真实三个 region 路由 + 源帧证据接口
  assert.ok(server.includes('_auto_proxy(\n                        "GET", f"/v1/devices/{device}/phone-search-region"))') ||
    /_auto_proxy\(\s*"GET", f"\/v1\/devices\/\{device\}\/phone-search-region"\)/.test(server),
    "GET 路由必须代理正式 phone-search-region");
  assert.match(server, /_auto_proxy\(\s*\n\s*"POST", f"\/v1\/devices\/\{device\}\/phone-search-region\/preview"/);
  assert.match(server, /_auto_proxy\(\s*\n\s*"PUT", f"\/v1\/devices\/\{device\}\/phone-search-region"/);
  assert.ok(server.includes("_region_source_frame"));
  assert.ok(server.includes("camera-source-frame"));
  // 页面只调调试台同源代理，不直连 8788，也不出现自由选相机入口
  assert.match(regionCode, /\/api\/phone-search-region\/preview/);
  assert.match(regionCode, /\/api\/phone-search-region\/save/);
  assert.match(regionCode, /\/api\/phone-search-region\/frame/);
  assert.doesNotMatch(regionCode, /8788|\/v1\/devices/);
  // 保存零动作：粗框只写资料；无机械/输入能力调用
  assert.match(html, /机械\/输入次数为 0/);
  assert.doesNotMatch(regionCode, /\/api\/jog|\/api\/tap\b|input_claim/);
});

// ---------------- F12-2：letterbox/变换正反转换与边缘舍入 ----------------
test("f12_2_letterbox_transform_roundtrip_and_edge_rounding", () => {
  const context = vm.createContext({Math, Number});
  const pick = name => script.match(new RegExp(`function ${name}[\\s\\S]*?\\n}`))[0];
  // 四个纯函数装进同一上下文：regionRectFromDom 内部调用 regionDomToSourcePoint
  vm.runInContext([pick("regionCanvasTransform"), pick("regionDomToSourcePoint"),
                   pick("regionSourceToDomPoint"), pick("regionRectFromDom")]
                   .join("\n"), context);
  const transform = (vw, vh, fw, fh, rot, mirror) => vm.runInContext(
    `regionCanvasTransform(${vw}, ${vh}, ${fw}, ${fh}` +
    `${rot !== undefined ? `, ${rot}, ${!!mirror}` : ""})`, context);
  const toSource = (x, y, t, w, h) => vm.runInContext(
    `regionDomToSourcePoint(${x}, ${y}, ${JSON.stringify(t)}, ${w}, ${h})`, context);
  const toDom = (sx, sy, t, w, h) => vm.runInContext(
    `regionSourceToDomPoint(${sx}, ${sy}, ${JSON.stringify(t)}, ${w}, ${h})`, context);
  const rectFrom = (x1, y1, x2, y2, t, w, h) => vm.runInContext(
    `regionRectFromDom(${x1}, ${y1}, ${x2}, ${y2}, ${JSON.stringify(t)}, ${w}, ${h})`,
    context);

  // 桌面：640x360 canvas，640x480 源帧 → 水平 letterbox（scale=min(1,0.75)）
  const t = transform(640, 360, 640, 480);
  assert.equal(t.scale, 0.75);
  assert.equal(t.offsetX, 80);
  assert.equal(t.offsetY, 0);
  // 正反转换：显示点 → 源点 → 显示点（同点往返）
  for (const [x, y] of [[t.offsetX, t.offsetY],
                        [t.offsetX + t.dispW, t.offsetY + t.dispH],
                        [320, 180]]) {
    const src = toSource(x, y, t, 640, 480);
    const back = toDom(src.x, src.y, t, 640, 480);
    assert.ok(Math.abs(back.x - x) < 1e-6 && Math.abs(back.y - y) < 1e-6,
      `roundtrip failed for (${x},${y})`);
  }
  // 窄屏：视口小于源图 → 整体缩小 + 居中
  const narrow = transform(320, 200, 1280, 720);
  assert.equal(narrow.scale, 320 / 1280);
  const src = toSource(160, 100, narrow, 1280, 720);
  assert.ok(Math.abs(src.x - 640) < 1e-6 && Math.abs(src.y - 360) < 1e-6);
  // 旋转 90 + 镜像：中心点往返不变；四角映射在界内
  const rot = transform(400, 400, 480, 640, 90, true);
  const center = toDom(240, 320, rot, 480, 640);
  const back = toSource(center.x, center.y, rot, 480, 640);
  assert.ok(Math.abs(back.x - 240) < 1e-6 && Math.abs(back.y - 320) < 1e-6);
  // 边缘舍入：左上 floor、右下 ceil，右/下排他；<4px 拒绝
  const flat = transform(640, 480, 640, 480);
  const rect = rectFrom(100.6, 50.2, 300.4, 200.9, flat, 640, 480);
  assert.equal(JSON.stringify(rect.pixel_rect),
    JSON.stringify({left: 100, top: 50, right: 301, bottom: 201}));
  // R2-15：页面 rect 域 = source_norm {x,y,width,height}（右/下排他）
  assert.equal(rect.x, 100 / 640);
  assert.equal(rect.y, 50 / 480);
  assert.equal(rect.width, (301 - 100) / 640);
  assert.equal(rect.height, (201 - 50) / 480);
  assert.equal(rectFrom(0, 0, 2.9, 300, flat, 640, 480), null,
    "<4px 宽（ceil 后 3px）必须拒绝");
  assert.equal(rectFrom(0, 0, 640, 2.9, flat, 640, 480), null,
    "<4px 高（ceil 后 3px）必须拒绝");
  assert.ok(rectFrom(0, 0, 640, 3.9, flat, 640, 480) !== null,
    "ceil 后恰 4px 是最小合法框");
  // 越界拖拽被钳制在源图范围内（无越界坐标）
  const clamped = rectFrom(-50, -50, 9999, 9999, flat, 640, 480);
  assert.equal(JSON.stringify(clamped.pixel_rect),
    JSON.stringify({left: 0, top: 0, right: 640, bottom: 480}));
});

// ---------------- F12-3：A→B→A 迟到回包不得覆盖当前上下文 ----------------
test("f12_3_abab_late_responses_cannot_clobber_current_context", async () => {
  const h = harness();
  const ctx = h.context;
  // A（第一次）：generation A=1，请求已发出但未回包
  const pA1 = ctx.regionSelectDevice("arm-01");
  await tick();
  assert.equal(h.pendingFor("phone-search-region?device=arm-01"), 1);
  // 未等 A 回包切到 B：generation B=1，旧图/草稿/preview 引用立即清空
  const pB = ctx.regionSelectDevice("arm-02");
  await tick();
  assert.equal(ctx.REGION.device, "arm-02");
  assert.equal(ctx.REGION.saved, null);
  assert.equal(ctx.REGION.frame, null);
  assert.equal(ctx.REGION.preview, null);
  assert.equal(ctx.REGION.framing, false);
  // B 先回包：正常显示 B 的资料
  h.resolve("phone-search-region?device=arm-02", {device_id: "arm-02", revision: 5, selected: null});
  await pB;
  assert.deepEqual(ctx.REGION.saved,
    {device_id: "arm-02", revision: 5, selected: null});
  // 切回 A（generation A=2），先让 A 的**第一次（过期）**回包到达
  const pA2 = ctx.regionSelectDevice("arm-01");
  await tick();
  h.resolve("phone-search-region?device=arm-01", {device_id: "arm-01", revision: 1, selected: null});
  await tick();
  await pA1;  // 旧上下文自行关闭
  assert.equal(ctx.REGION.saved, null,
    "A→B→A：过期 generation 的回包不能覆盖当前上下文");
  assert.equal(ctx.REGION.device, "arm-01");
  // A 的第二次（当前）回包才被采纳
  h.resolve("phone-search-region?device=arm-01",
    {device_id: "arm-01", revision: 3,
     selected: {version: 2, record: {installation_binding: {camera_id: "cam-02"}}}});
  await pA2;
  assert.equal(ctx.REGION.saved.revision, 3);
  assert.equal(ctx.REGION.saved.device_id, "arm-01");
  // B 的资料不受影响：B 请求只发过一次（无重复写），且当前上下文属于 A
  assert.equal(h.pendingFor("phone-search-region?device=arm-02"), 0);
});

test("f12_3_stale_frame_context_closes_only_itself_and_blocks_actions", async () => {
  const h = harness();
  const ctx = h.context;
  // A 上开始框选（取源帧未回包）
  const pOpen = ctx.regionOpen();
  await tick();
  assert.equal(h.pendingFor("/frame?device=arm-01"), 1);
  assert.equal(ctx.REGION.busy, true);
  // 切到 B：框选立即失效（不等于新响应后再清）
  const pSel = ctx.regionSelectDevice("arm-02");
  await tick();
  assert.equal(ctx.REGION.framing, false);
  // A 的源帧现在才回包：迟到上下文只关闭自身，不影响 B
  h.resolve("/frame?device=arm-01", {
    evidence_id: "ev-A", jpeg_base64: "AAAA", jpeg_sha256: "sha-A",
    captured_at: "2026-10-03T00:00:00Z",
    source_frame: {frame_width: 640, frame_height: 480}});
  await pOpen;
  assert.equal(ctx.REGION.frame, null, "迟到源帧不能落到已切走的上下文");
  assert.equal(ctx.REGION.device, "arm-02");
  // B 回包完成切换
  h.resolve("phone-search-region?device=arm-02", {device_id: "arm-02", revision: 0, selected: null});
  await pSel;
  // 新设备上下文里画框/预览/保存均不可用（无冻结帧、无 preview_id）
  assert.equal(ctx.REGION.rect, null);
  assert.equal(ctx.REGION.preview, null);
  const click = h.elements["#regionCanvas"].listeners.pointerdown;
  click[0]({clientX: 10, clientY: 10});
  assert.equal(ctx.REGION.drag, null, "新设备回包前拖拽不能操作旧图");
  assert.equal(h.calls.filter(c => c.path.includes("/save")).length, 0,
    "迟到上下文不得下发保存");
  // 框选期间移动/触屏被禁用（源代码级：motion 与 camwrap 都检查 REGION.framing）
  assert.match(script, /REGION\.framing\)\s*\{\s*[\s\S]{0,200}框选进行中/s);
});

// ---------------- F12-4：无校准可画框 + 真实版本展示 ----------------
test("f12_4_framing_without_calibration_and_honest_version_display", () => {
  // 粗框模块不读取校准门禁（calibration/ready/validated），新设备无校准能画框
  assert.doesNotMatch(regionCode,
    /calibration_status|calibrationReady|validated|bundle/);
  // 保存后显示真实版本与绑定，不伪装「校准已通过」。
  // close-A 修正：写契约返回 saved_version/selected_version（旧代码误读
  // result.version 显示 vundefined）；此处断言修正后的真实版本读取。
  assert.match(regionCode,
    /saved_version \?\? .*selected_version \?\? .*\.version/s);
  assert.match(regionCode, /已保存版本 v\$\{savedVersion\}/);
  assert.match(regionCode, /不代表校准已通过/);
  // 发布/草稿语义来自服务端契约（publish 由请求方携带；预览单次消费）
  assert.match(regionCode, /publish: publish !== false/);
  assert.match(regionCode, /REGION\.preview = null;\s*\/\/ 预览单次消费/);
  // 保存需要 preview（preview_id/region_digest/expected_revision 全携带）
  assert.match(regionCode, /preview_id: REGION\.preview\.preview_id/);
  assert.match(regionCode, /region_digest: REGION\.preview\.region_digest/);
  assert.match(regionCode, /expected_revision:/);
  // 源帧冻结带证据身份（evidence_id/sha256），不拿组件宽高当原帧尺寸
  assert.match(regionCode, /evidence_id/);
  assert.match(regionCode, /frame_width/);
});
