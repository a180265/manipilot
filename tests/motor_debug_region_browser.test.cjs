// Full production HTML, real DOM events, and an entirely intercepted HTTP boundary.
// No Fleet process, database, camera, IME, or hardware is used by this suite.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const {test, before, after} = require("node:test");
const {chromium} = require("../../frontend/node_modules/playwright-core");

const htmlPath = path.join(__dirname, "../scripts/motor_debug_page.html");
const html = fs.readFileSync(htmlPath, "utf8");
const origin = "http://roi-isolated.invalid";
const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), "roi-browser-review-"));
const results = [];
let browser;

before(async () => {
  browser = await chromium.launch({
    executablePath: process.env.ROI_TEST_CHROME ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
    args: ["--disable-background-networking", "--disable-component-update",
      "--disable-sync", "--no-pings", "--host-resolver-rules=MAP * ~NOTFOUND"],
  });
});

after(async () => {
  if (browser) await browser.close();
  fs.writeFileSync(path.join(artifacts, "results.json"), JSON.stringify({
    html_sha256: crypto.createHash("sha256").update(html).digest("hex"),
    results,
  }, null, 2));
  console.log("ROI_BROWSER_ARTIFACTS=" + artifacts);
});

async function eventually(check, message = "condition was not met") {
  let error;
  for (let i = 0; i < 100; i += 1) {
    try { if (await check()) return; } catch (e) { error = e; }
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  throw error || new Error(message);
}

async function fixture(t, {width = 1280, height = 900, frameW = 1920, frameH = 1080} = {}) {
  const context = await browser.newContext({
    viewport: {width, height}, offline: true, serviceWorkers: "block",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  const jpeg = await page.evaluate(({w, h}) => {
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#123436"; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = "#f4f4f4"; ctx.fillRect(w * .2, h * .1, w * .5, h * .8);
    ctx.fillStyle = "#dc3a57"; ctx.fillRect(w * .3, h * .2, w * .1, h * .1);
    return canvas.toDataURL("image/jpeg").split(",")[1];
  }, {w: frameW, h: frameH});
  const calls = [], hooks = [], held = new Set(), unexpected = [], errors = [];
  const records = new Map(["arm-01", "arm-02"].map(device => [device, {
    revision: 0, selected: null, pending_draft: null, status: "unregistered", reasons: [],
  }]));
  const previews = new Map();
  let contextSeq = 0, frameSeq = 0, previewSeq = 0;
  page.on("pageerror", error => errors.push(error.message));

  const json = (body, status = 200) => ({
    status, contentType: "application/json", body: JSON.stringify(body),
  });
  function hold(method, pathname, predicate = () => true) {
    const entry = {method, pathname, predicate, request: null};
    hooks.push(entry);
    return entry;
  }
  function responseFor(req) {
    if (req.pathname === "/api/debug-session") return json({csrf_token: "isolated-csrf"});
    if (req.pathname === "/api/devices") return json({devices: ["arm-01", "arm-02"].map(deviceId => ({
      deviceId, dynamic: true, driverMode: "simulation", cameraId: "cam-" + deviceId,
    }))});
    if (req.pathname === "/api/device-context") return json({
      deviceId: req.body.deviceId, contextId: "ctx-" + (++contextSeq),
      cameraId: "cam-" + req.body.deviceId, cameraDeviceUniqueId: "physical-" + req.body.deviceId,
      bindingRevision: 1, frameWidth: frameW, frameHeight: frameH, checks: [], error: null,
    });
    if (req.pathname.startsWith("/api/device-context/")) return json({closed: true});
    if (req.pathname === "/api/camera/stream") {
      return {status: 200, contentType: "image/jpeg", body: Buffer.from(jpeg, "base64")};
    }
    if (req.pathname === "/api/gesture-profiles") return json({entry: null});
    if (req.pathname === "/api/screen-proof") return json({profile: null, message: "未登记"});
    if (req.pathname === "/api/phone-search-region") return json(records.get(req.device));
    if (req.pathname === "/api/phone-search-region/version") return json({
      record: {installation_binding: {camera_id: "cam-" + req.device, camera_binding_id: "binding-1"}},
    });
    if (req.pathname === "/api/phone-search-region/frame") return json({
      device_id: req.device, evidence_id: "frame-" + (++frameSeq), jpeg_base64: jpeg,
      jpeg_sha256: crypto.createHash("sha256").update(Buffer.from(jpeg, "base64")).digest("hex"),
      source_frame: {frame_width: frameW, frame_height: frameH},
    });
    if (req.pathname === "/api/phone-search-region/preview") {
      const rect = req.body.rect;
      const preview = {
        preview_id: "preview-" + (++previewSeq), region_digest: "digest-" + previewSeq,
        status: "verified", reason_codes: [], rect,
        pixel_rect: {left: Math.floor(rect.x * frameW), top: Math.floor(rect.y * frameH),
          right: Math.ceil((rect.x + rect.width) * frameW),
          bottom: Math.ceil((rect.y + rect.height) * frameH)},
        expires_at: new Date(Date.now() + 120000).toISOString(),
        source_binding: {device_id: req.device, evidence_id: "fresh-detection-frame"},
      };
      previews.set(preview.preview_id, preview);
      return json(preview);
    }
    if (req.pathname === "/api/phone-search-region/save") {
      const saved = records.get(req.device);
      const version = saved.revision + 1;
      if (req.body.publish) {
        Object.assign(saved, {revision: version, selected: {version}, status: "verified"});
      } else {
        saved.pending_draft = {version};
        if (!saved.selected)
          Object.assign(saved, {status: "needs_review", reasons: ["phone_not_located"]});
      }
      return json({saved_version: version, selected_version: saved.selected?.version ?? null,
        revision: saved.revision, status: req.body.publish ? "verified" : "draft_saved"});
    }
    // Other panels are present and execute their real handlers, but have no backend here.
    if (req.method !== "GET") unexpected.push(req.method + " " + req.pathname);
    return json({error: {code: "isolated_panel_unavailable", message: "隔离测试未连接此模块"}}, 503);
  }

  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) {
      unexpected.push(request.url());
      await route.abort();
      return;
    }
    if (url.pathname === "/") {
      await route.fulfill({status: 200, contentType: "text/html", body: html});
      return;
    }
    let body = {};
    try { body = request.postDataJSON() || {}; } catch (_) {}
    const req = {method: request.method(), pathname: url.pathname, body,
      device: body.deviceId || url.searchParams.get("device") || url.searchParams.get("deviceId"),
      query: Object.fromEntries(url.searchParams)};
    calls.push(req);
    const index = hooks.findIndex(h => h.method === req.method &&
      h.pathname === req.pathname && h.predicate(req));
    if (index >= 0) {
      const hook = hooks.splice(index, 1)[0];
      hook.request = req;
      hook.response = responseFor(req);
      held.add(hook);
      await new Promise(resolve => {
        hook.release = async response => {
          held.delete(hook);
          try { await route.fulfill(response || hook.response); } finally { resolve(); }
        };
      });
    } else {
      await route.fulfill(responseFor(req));
    }
  });
  t.after(async () => {
    try {
      for (const hook of held) await hook.release().catch(() => {});
      await context.close();
      assert.deepEqual(unexpected, [], "no hardware writes or unintercepted network requests");
      assert.deepEqual(errors, [], "the complete page must not throw uncaught errors");
    } catch (error) {
      Object.assign(t.roiResult, {passed: false, error: error.message});
      throw error;
    }
  });
  await page.goto(origin + "/?deviceId=arm-01", {waitUntil: "load"});
  await eventually(() => page.locator("#regionMsg").textContent().then(s => s.includes("arm-01")));
  await page.waitForFunction(() => REGION.saved !== null && !REGION.busy);
  return {
    page, calls, records, hold, json, frameW, frameH,
    async open() {
      await page.locator("#btnRegionOpen").click();
      await page.waitForFunction(() => REGION.frame !== null && !REGION.busy);
    },
    async draw(points = [300, 200, 1500, 900], release = true) {
      await page.locator("#regionCanvas").scrollIntoViewIfNeeded();
      const box = await page.locator("#regionCanvas").evaluate(canvas => {
        const r = canvas.getBoundingClientRect(), css = getComputedStyle(canvas);
        const l = parseFloat(css.borderLeftWidth), t = parseFloat(css.borderTopWidth);
        return {x: r.left + l, y: r.top + t,
          w: r.width - l - parseFloat(css.borderRightWidth),
          h: r.height - t - parseFloat(css.borderBottomWidth)};
      });
      const scale = Math.min(box.w / frameW, box.h / frameH);
      const x = sx => box.x + (box.w - frameW * scale) / 2 + sx * scale;
      const y = sy => box.y + (box.h - frameH * scale) / 2 + sy * scale;
      await page.mouse.move(x(points[0]), y(points[1]));
      await page.mouse.down();
      await page.mouse.move(x(points[2]), y(points[3]), {steps: 4});
      if (release) await page.mouse.up();
    },
    async preview() {
      await page.locator("#btnRegionPreview").click();
      await page.waitForFunction(() => REGION.preview !== null && !REGION.busy);
    },
    async screenshot(name) {
      const file = path.join(artifacts, name + ".png");
      await page.screenshot({path: file});
      return file;
    },
  };
}

function browserTest(name, fn) {
  test(name, async t => {
    const result = {name, passed: false};
    results.push(result);
    t.roiResult = result;
    try { await fn(t); result.passed = true; }
    catch (error) { result.error = error.message; throw error; }
  });
}

browserTest("latest real dropdown selection wins delayed A-B-A context teardown", async t => {
  const h = await fixture(t);
  const contextId = await h.page.evaluate(() => S.contextId);
  const closing = h.hold("DELETE", "/api/device-context/" + contextId);
  await h.page.selectOption("#deviceId", "arm-02");
  await eventually(() => closing.request);
  await h.page.selectOption("#deviceId", "arm-01");
  await h.page.waitForFunction(() => S.contextId && REGION.device === "arm-01" && !REGION.busy);
  await closing.release();
  await h.page.waitForTimeout(150);
  await h.open();
  assert.equal(await h.page.locator("#deviceId").inputValue(), "arm-01");
  assert.equal(h.calls.filter(r => r.pathname.endsWith("/frame")).at(-1).device, "arm-01");
  assert.match(await h.page.locator("#deviceContext").textContent(), /机械臂 arm-01/);
});

browserTest("redrawing through real pointer events immediately invalidates old preview", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  await h.draw([600, 300, 1700, 1000], false);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  await h.page.mouse.up();
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  await h.preview();
  await h.page.locator("#btnRegionSave").click();
  await eventually(() => h.calls.some(r => r.pathname.endsWith("/save")));
  assert.equal(h.calls.filter(r => r.pathname.endsWith("/save")).at(-1).body.preview_id, "preview-2");
});

browserTest("camera identity failure revokes the old frame and publish credential", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const frame = h.hold("GET", "/api/phone-search-region/frame");
  await h.page.locator("#btnRegionOpen").click();
  await eventually(() => frame.request);
  await frame.release(h.json({error: {code: "camera_unique_id_changed",
    message: "camera_unique_id_changed"}}, 409));
  await h.page.waitForFunction(() => !REGION.busy);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.evaluate(() => REGION.frame), null);
  assert.equal(await h.page.evaluate(() => REGION.preview), null);
});

browserTest("actual polling recovery cannot accept a preview from the previous context", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw();
  const oldContext = await h.page.evaluate(() => S.contextId);
  const preview = h.hold("POST", "/api/phone-search-region/preview");
  await h.page.locator("#btnRegionPreview").click();
  await eventually(() => preview.request);
  const poll = h.hold("GET", "/api/camera/status");
  await eventually(() => poll.request, "camera poll did not occur");
  await poll.release(h.json({error: {code: "device_context_expired",
    message: "device_context_expired"}}, 409));
  await h.page.waitForFunction(old => S.contextId && S.contextId !== old, oldContext);
  await preview.release();
  await h.page.waitForFunction(() => !REGION.busy);
  assert.equal(await h.page.evaluate(() => REGION.preview), null);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
});

browserTest("late save reread for A cannot put a saved-version message on B", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const reread = h.hold("GET", "/api/phone-search-region", r => r.device === "arm-01");
  await h.page.locator("#btnRegionSave").click();
  await eventually(() => reread.request);
  await h.page.selectOption("#deviceId", "arm-02");
  await h.page.waitForFunction(() => REGION.device === "arm-02" && REGION.saved !== null);
  await reread.release();
  await h.page.waitForTimeout(100);
  assert.doesNotMatch(await h.page.locator("#regionMsg").textContent(), /已保存版本|已发布范围/);
  assert.equal(await h.page.evaluate(() => REGION.saved.revision), 0);
});

for (const width of [1280, 390]) {
  browserTest(`real canvas content coordinates exclude borders at viewport ${width}`, async t => {
    const h = await fixture(t, {width});
    await h.open(); await h.draw([0, 0, 1500, 900]); await h.preview();
    const rect = h.calls.filter(r => r.pathname.endsWith("/preview")).at(-1).body.rect;
    const actual = [rect.x * h.frameW, rect.y * h.frameH,
      (rect.x + rect.width) * h.frameW, (rect.y + rect.height) * h.frameH];
    const screenshot = await h.screenshot("canvas-" + width);
    const pixel = await h.page.locator("#regionCanvas").evaluate(canvas =>
      Array.from(canvas.getContext("2d").getImageData(320, 180, 1, 1).data));
    t.diagnostic(JSON.stringify({actual, screenshot, pixel}));
    assert.ok(pixel[0] > 100 && pixel[3] === 255, "source image is visibly rendered");
    for (const [i, expected] of [0, 0, 1500, 900].entries()) {
      assert.ok(Math.abs(actual[i] - expected) <= 1,
        `source edge ${i}: expected ${expected}, got ${actual[i]}`);
    }
  });
}

browserTest("needs-review preview can only be explicitly saved as an unpublished draft", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw();
  const preview = h.hold("POST", "/api/phone-search-region/preview");
  await h.page.locator("#btnRegionPreview").click();
  await eventually(() => preview.request);
  const payload = JSON.parse(preview.response.body);
  await preview.release(h.json({...payload, status: "needs_review",
    reason_codes: ["multiple_screen_candidates"], detected_quad: null}));
  await h.page.waitForFunction(() => !REGION.busy);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.match(await h.page.locator("#regionMsg").textContent(), /待复核/);
  assert.doesNotMatch(await h.page.locator("#regionMsg").textContent(), /预览有效/);
  await h.page.locator("#btnRegionDraft").click();
  await eventually(() => h.calls.some(r => r.pathname.endsWith("/save")));
  const write = h.calls.filter(r => r.pathname.endsWith("/save")).at(-1);
  assert.equal(write.body.publish, false);
  await h.page.waitForFunction(() => !REGION.busy && REGION.saved?.pending_draft);
  assert.match(await h.page.locator("#regionMsg").textContent(), /草稿.*未发布/);
  assert.doesNotMatch(await h.page.locator("#regionBindingText").textContent(), /当前生效：v1/);
  assert.equal(h.records.get("arm-01").selected, null);
});

browserTest("late source frame after A-B-A cannot replace the current device frame", async t => {
  const h = await fixture(t);
  const frame = h.hold("GET", "/api/phone-search-region/frame");
  await h.page.locator("#btnRegionOpen").click();
  await eventually(() => frame.request);
  await h.page.selectOption("#deviceId", "arm-02");
  await h.page.waitForFunction(() => REGION.device === "arm-02" && REGION.ready);
  await h.page.selectOption("#deviceId", "arm-01");
  await h.page.waitForFunction(() => REGION.device === "arm-01" && REGION.ready);
  await h.open();
  await frame.release();
  await h.draw(); await h.preview();
  const request = h.calls.filter(r => r.pathname.endsWith("/preview")).at(-1);
  assert.equal(request.body.source_evidence_id, "frame-2");
});

browserTest("stale context creation closes only its own context after a newer selection", async t => {
  const h = await fixture(t);
  const create = h.hold("POST", "/api/device-context", r => r.device === "arm-02");
  await h.page.selectOption("#deviceId", "arm-02");
  await eventually(() => create.request);
  const staleId = JSON.parse(create.response.body).contextId;
  await h.page.selectOption("#deviceId", "arm-01");
  await h.page.waitForFunction(() => REGION.device === "arm-01" && REGION.ready);
  await create.release();
  await eventually(() => h.calls.some(r =>
    r.method === "DELETE" && r.pathname === "/api/device-context/" + staleId));
  await h.open();
  assert.equal(h.calls.filter(r => r.pathname.endsWith("/frame")).at(-1).device, "arm-01");
});

browserTest("late CAS-conflict reread cannot overwrite the selected device message", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const save = h.hold("POST", "/api/phone-search-region/save");
  const reread = h.hold("GET", "/api/phone-search-region", r => r.device === "arm-01");
  await h.page.locator("#btnRegionSave").click();
  await eventually(() => save.request);
  await save.release(h.json({error: {code: "layout_revision_conflict",
    message: "版本冲突"}}, 409));
  await eventually(() => reread.request);
  await h.page.selectOption("#deviceId", "arm-02");
  await h.page.waitForFunction(() => REGION.device === "arm-02" && REGION.ready);
  await reread.release();
  await h.page.waitForTimeout(100);
  assert.doesNotMatch(await h.page.locator("#regionMsg").textContent(), /版本冲突|保存失败/);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
});

browserTest("an expired preview disables both write choices without sending a request", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw();
  const preview = h.hold("POST", "/api/phone-search-region/preview");
  await h.page.locator("#btnRegionPreview").click();
  await eventually(() => preview.request);
  await preview.release(h.json({...JSON.parse(preview.response.body),
    expires_at: new Date(Date.now() + 200).toISOString()}));
  await h.page.waitForFunction(() => REGION.preview !== null);
  await h.page.waitForFunction(() => REGION.preview === null);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionDraft").isDisabled(), true);
  assert.match(await h.page.locator("#regionMsg").textContent(), /过期/);
  assert.equal(h.calls.filter(r => r.pathname.endsWith("/save")).length, 0);
});

browserTest("a failed new preview cannot leave a previously verified preview writable", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const preview = h.hold("POST", "/api/phone-search-region/preview");
  await h.page.locator("#btnRegionPreview").click();
  await eventually(() => preview.request);
  await preview.release(h.json({error: {code: "source_unavailable", message: "无新帧"}}, 503));
  await h.page.waitForFunction(() => !REGION.busy);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionDraft").isDisabled(), true);
});

browserTest("successive published saves reread the real revision and never reuse a preview", async t => {
  const h = await fixture(t);
  for (let version = 1; version <= 2; version += 1) {
    await h.open(); await h.draw(); await h.preview();
    await h.page.locator("#btnRegionSave").click();
    await h.page.waitForFunction(v => REGION.ready && !REGION.busy &&
      REGION.saved?.revision === v, version);
    assert.match(await h.page.locator("#regionMsg").textContent(), new RegExp("已发布范围 v" + version));
  }
  const writes = h.calls.filter(r => r.pathname.endsWith("/save"));
  assert.deepEqual(writes.map(r => r.body.expected_revision), [0, 1]);
  assert.deepEqual(writes.map(r => r.body.preview_id), ["preview-1", "preview-2"]);
  assert.deepEqual(writes.map(r => r.body.publish), [true, true]);
});

browserTest("pointer cancellation and a new click cannot retain a prior rectangle", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  await h.draw([600, 300, 1500, 900], false);
  await h.page.locator("#regionCanvas").dispatchEvent("pointercancel", {pointerId: 1});
  await h.page.mouse.up();
  assert.equal(await h.page.locator("#btnRegionPreview").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  await h.draw([500, 300, 500, 300]);
  assert.equal(await h.page.locator("#btnRegionPreview").isDisabled(), true);
});

browserTest("source metadata dimensions must match the actually decoded original image", async t => {
  const h = await fixture(t);
  const frame = h.hold("GET", "/api/phone-search-region/frame");
  await h.page.locator("#btnRegionOpen").click();
  await eventually(() => frame.request);
  const source = JSON.parse(frame.response.body);
  await frame.release(h.json({...source, source_frame: {frame_width: 640, frame_height: 360}}));
  await h.page.waitForFunction(() => !REGION.busy);
  assert.equal(await h.page.evaluate(() => REGION.frame), null);
  assert.equal(await h.page.locator("#btnRegionPreview").isDisabled(), true);
  assert.match(await h.page.locator("#regionMsg").textContent(), /尺寸不一致/);
});

browserTest("a mismatched preview rectangle cannot authorize a save of the drawn rectangle", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw();
  const preview = h.hold("POST", "/api/phone-search-region/preview");
  await h.page.locator("#btnRegionPreview").click();
  await eventually(() => preview.request);
  const payload = JSON.parse(preview.response.body);
  await preview.release(h.json({...payload, rect: {...payload.rect, x: .8}}));
  await h.page.waitForFunction(() => !REGION.busy);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionDraft").isDisabled(), true);
});

browserTest("old saved-range response cannot clobber a later A-B-A read", async t => {
  const h = await fixture(t);
  const old = h.hold("GET", "/api/phone-search-region", r => r.device === "arm-01");
  await h.page.locator("#btnRegionRefresh").click();
  await eventually(() => old.request);
  await h.page.selectOption("#deviceId", "arm-02");
  await h.page.waitForFunction(() => REGION.device === "arm-02" && REGION.ready);
  await h.page.selectOption("#deviceId", "arm-01");
  await h.page.waitForFunction(() => REGION.device === "arm-01" && REGION.ready);
  await old.release(h.json({revision: 99, selected: {version: 99},
    status: "verified", reasons: [], pending_draft: null}));
  await h.page.waitForTimeout(100);
  assert.equal(await h.page.evaluate(() => REGION.saved.revision), 0);
  assert.doesNotMatch(await h.page.locator("#regionBindingText").textContent(), /v99/);
});

browserTest("late A save completion cannot release busy ownership of a new B preview", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const save = h.hold("POST", "/api/phone-search-region/save");
  await h.page.locator("#btnRegionSave").click();
  await eventually(() => save.request);
  await h.page.selectOption("#deviceId", "arm-02");
  await h.page.waitForFunction(() => REGION.device === "arm-02" && REGION.ready);
  await h.open(); await h.draw();
  const preview = h.hold("POST", "/api/phone-search-region/preview");
  await h.page.locator("#btnRegionPreview").click();
  await eventually(() => preview.request);
  await save.release();
  await h.page.waitForTimeout(100);
  assert.equal(await h.page.locator("#btnRegionOpen").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  await preview.release();
  await h.page.waitForFunction(() => !REGION.busy && REGION.preview !== null);
  assert.equal(await h.page.locator("#btnRegionSave").isEnabled(), true);
  assert.doesNotMatch(await h.page.locator("#regionMsg").textContent(), /已发布范围/);
});

browserTest("binding-change refresh establishes a new context but requires a new frame and preview", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const oldContext = await h.page.evaluate(() => S.contextId);
  const poll = h.hold("GET", "/api/camera/status");
  await eventually(() => poll.request);
  await poll.release(h.json({error: {code: "camera_binding_revision_changed",
    message: "camera_binding_revision_changed"}}, 409));
  await h.page.waitForFunction(() => REGION.invalidReason !== "");
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  await h.page.locator("#btnRegionRefresh").click();
  await h.page.waitForFunction(old => REGION.ready && !REGION.busy &&
    S.contextId !== old, oldContext);
  assert.equal(await h.page.locator("#btnRegionPreview").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  await h.open(); await h.draw(); await h.preview();
  assert.equal(await h.page.locator("#btnRegionSave").isEnabled(), true);
});

browserTest("expiration during recovery reread cannot await its own recovery or leave controls busy", async t => {
  const h = await fixture(t);
  const poll = h.hold("GET", "/api/camera/status");
  await eventually(() => poll.request);
  const reread = h.hold("GET", "/api/phone-search-region");
  await poll.release(h.json({error: {code: "device_context_expired",
    message: "device_context_expired"}}, 410));
  await eventually(() => reread.request);
  await reread.release(h.json({error: {code: "device_context_expired",
    message: "device_context_expired"}}, 410));
  await h.page.waitForFunction(() => contextRecovery === null && !REGION.busy);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionRefresh").isEnabled(), true);
  await h.page.locator("#btnRegionRefresh").click();
  await h.page.waitForFunction(() => REGION.ready && !REGION.busy && contextRecovery === null);
  await h.open(); await h.draw(); await h.preview();
  assert.equal(await h.page.locator("#btnRegionSave").isEnabled(), true);
});

browserTest("draft saving preserves an already published selection and displays both facts", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  await h.page.locator("#btnRegionSave").click();
  await h.page.waitForFunction(() => REGION.ready && !REGION.busy && REGION.saved?.revision === 1);
  await h.open(); await h.draw(); await h.preview();
  await h.page.locator("#btnRegionDraft").click();
  await h.page.waitForFunction(() => REGION.ready && !REGION.busy && REGION.saved?.pending_draft);
  assert.equal(h.calls.filter(r => r.pathname.endsWith("/save")).at(-1).body.publish, false);
  assert.match(await h.page.locator("#regionMsg").textContent(), /草稿 v2（未发布）.*生效范围仍为 v1/);
  assert.match(await h.page.locator("#regionBindingText").textContent(), /当前生效：v1.*草稿 v2（未发布）/);
  assert.equal(h.records.get("arm-01").revision, 1);
});

browserTest("a persisted needs-review range is displayed as invalid rather than currently usable", async t => {
  const h = await fixture(t);
  h.records.set("arm-01", {revision: 7, selected: {version: 7}, status: "needs_review",
    reasons: ["source_binding_changed"], pending_draft: null});
  await h.page.locator("#btnRegionRefresh").click();
  await h.page.waitForFunction(() => REGION.ready && !REGION.busy && REGION.saved?.revision === 7);
  assert.match(await h.page.locator("#regionStageBadge").textContent(), /待复核/);
  assert.match(await h.page.locator("#regionBindingText").textContent(), /v7.*失效/);
  assert.doesNotMatch(await h.page.locator("#regionBindingText").textContent(), /当前生效/);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionOpen").isEnabled(), true);
});

browserTest("ambiguous write response never claims failure or permits a duplicate save", async t => {
  const h = await fixture(t);
  await h.open(); await h.draw(); await h.preview();
  const save = h.hold("POST", "/api/phone-search-region/save");
  await h.page.locator("#btnRegionSave").click();
  await eventually(() => save.request);
  await save.release(h.json({}));
  await h.page.waitForFunction(() => !REGION.busy);
  assert.match(await h.page.locator("#regionMsg").textContent(), /保存结果未知/);
  assert.doesNotMatch(await h.page.locator("#regionMsg").textContent(), /保存失败|已发布/);
  assert.equal(await h.page.locator("#btnRegionSave").isDisabled(), true);
  assert.equal(await h.page.locator("#btnRegionDraft").isDisabled(), true);
  assert.equal(h.calls.filter(r => r.pathname.endsWith("/save")).length, 1);
});

for (const [frameW, frameH, width] of [[1280, 960, 1280], [1080, 1920, 390]]) {
  browserTest(`letterboxed ${frameW}x${frameH} original maps to source pixels at viewport ${width}`, async t => {
    const h = await fixture(t, {frameW, frameH, width});
    await h.open(); await h.draw([100, 120, 800, 900]); await h.preview();
    const rect = h.calls.filter(r => r.pathname.endsWith("/preview")).at(-1).body.rect;
    const actual = [rect.x * frameW, rect.y * frameH,
      (rect.x + rect.width) * frameW, (rect.y + rect.height) * frameH];
    for (const [i, expected] of [100, 120, 800, 900].entries())
      assert.ok(Math.abs(actual[i] - expected) <= 1, `${actual} != source rectangle`);
    t.diagnostic(JSON.stringify({actual, screenshot: await h.screenshot(`letterbox-${frameW}-${width}`)}));
  });
}
