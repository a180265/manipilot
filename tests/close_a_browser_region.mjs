// close-A：调试台粗框真实浏览器链（R4-02-A..E / R3-15-A..C / R4-08-D H01）。
// 真实 Chrome（channel）+ 真实鼠标拖框；断言走正式 HTTP 请求与页面可见状态。
// 不调用页面内部 JS 函数冒充用户操作；只读 DOM（文本/按钮态/几何）作断言。
//
// 用法：node close_a_browser_region.mjs <phase> <consoleBase> <outJson> <shotsDir>
//        <frontendRoot> <controlBase> [extraBase]
// phase：main | letterbox | camera-switch | restart | h01 | h01-restart
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const [, , phase, base, outJson, shotsDir, frontendRoot, controlBase, extraBase] =
  process.argv
const { chromium } = createRequire(resolve(frontendRoot, 'package.json'))('playwright-core')
mkdirSync(shotsDir, { recursive: true })

const cases = []
const requests = []
let softFailures = 0
async function step(case_id, title, fn, { fatal = false } = {}) {
  const started = Date.now()
  try {
    const detail = (await fn()) || {}
    cases.push({ case_id, title, status: 'passed', duration_ms: Date.now() - started, detail })
  } catch (error) {
    cases.push({ case_id, title, status: 'failed', duration_ms: Date.now() - started,
                 error: String(error && error.message || error) })
    if (fatal) throw error
    softFailures += 1
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
const apiResponses = {}
page.on('response', async res => {
  const url = res.url()
  requests.push({ method: res.request().method(), url, status: res.status() })
  if (/\/api\/phone-search-region\/(preview|save)$/.test(url) && res.status() < 500) {
    try { apiResponses[`${res.request().method()} ${url.split('/api/')[1]}#${requests.length}`] =
      { status: res.status(), body: await res.json() } } catch { /* 非 JSON 忽略 */ }
  }
})
const shot = name => page.screenshot({ path: resolve(shotsDir, `${name}.png`), fullPage: false })
const $ = sel => page.locator(sel)
const text = async sel => (await $(sel).textContent()) || ''
const msgText = () => text('#regionMsg')
const bindingText = () => text('#regionBindingText')
const stageText = () => text('#regionStageBadge')

// —— 独立 letterbox 几何（测试自算，不调页面函数）——
// 画布属性宽高 cw,ch（从 DOM 读）；源帧 fw,fh；scale=min(cw/fw,ch/fh)，
// 源点 → canvas 点 (offX+s*sx, offY+s*sy)；canvas 点 → client 按 boundingRect 比例。
function sourceToClient(rect, canvasBox, cw, ch, fw, fh) {
  const scale = Math.min(cw / fw, ch / fh)
  const offX = (cw - fw * scale) / 2
  const offY = (ch - fh * scale) / 2
  return { x: canvasBox.x + (offX + scale * rect.sx) * (canvasBox.width / cw),
           y: canvasBox.y + (offY + scale * rect.sy) * (canvasBox.height / ch) }
}

async function canvasDrag(rect, fw = 640, fh = 480) {
  const handle = $('#regionCanvas')
  await handle.scrollIntoViewIfNeeded()
  const box = await handle.boundingBox()
  const cw = Number(await handle.evaluate(el => el.width))
  const chh = Number(await handle.evaluate(el => el.height))
  const a = sourceToClient({ sx: rect.sx, sy: rect.sy }, box, cw, chh, fw, fh)
  const b = sourceToClient({ sx: rect.ex, sy: rect.ey }, box, cw, chh, fw, fh)
  await page.mouse.move(a.x, a.y)
  await page.mouse.down()
  for (let i = 1; i <= 6; i++) {
    await page.mouse.move(a.x + (b.x - a.x) * i / 6, a.y + (b.y - a.y) * i / 6)
  }
  await page.mouse.move(b.x, b.y)
  await page.mouse.up()
}

async function waitMsg(substr, timeout = 12000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (((await msgText()) || '').includes(substr)) return (await msgText())
    await page.waitForTimeout(120)
  }
  throw new Error(`等待 #regionMsg 出现「${substr}」超时；当前：${await msgText()}`)
}

async function waitMsgAny(subs, timeout = 15000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const current = (await msgText()) || ''
    for (const s of subs) if (current.includes(s)) return current
    await page.waitForTimeout(120)
  }
  throw new Error(`等待 #regionMsg 出现 ${JSON.stringify(subs)} 之一超时；当前：${await msgText()}`)
}

async function waitMsgRe(pattern, timeout = 12000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const current = (await msgText()) || ''
    if (pattern.test(current)) return current
    await page.waitForTimeout(120)
  }
  throw new Error(`等待 #regionMsg 匹配 ${pattern} 超时；当前：${await msgText()}`)
}

async function openFrameDrag(rect, fw, fh) {
  await $('#btnRegionOpen').click()
  await waitMsg('已冻结源帧')
  await canvasDrag(rect, fw, fh)
  await page.waitForTimeout(250)
}

async function clickPreview(expectVerified = true) {
  // 旧提示（“点「预览确认」后再保存”）也含“预览”，必须等新结果文案
  await $('#btnRegionPreview').click()
  const m = await waitMsgRe(/预览有效|预览失败/, 15000)
  if (expectVerified && !/预览有效/.test(m)) throw new Error(`预览未通过：${m}`)
  return m
}

async function clickSave(expectVersion, expectRevision) {
  await $('#btnRegionSave').click()
  const m = await waitMsgRe(/已保存版本 v\d+|保存失败/, 20000)
  if (expectVersion !== undefined && !m.includes(`已保存版本 v${expectVersion}`))
    throw new Error(`期望保存版本 v${expectVersion}，页面：${m}`)
  if (expectRevision !== undefined && !m.includes(`revision ${expectRevision}`))
    throw new Error(`期望指针 revision ${expectRevision}，页面：${m}`)
  return m
}

async function buttonStates() {
  const ids = ['btnRegionOpen', 'btnRegionPreview', 'btnRegionSave', 'btnRegionRefresh']
  const states = {}
  for (const id of ids) states[id] = await $('#' + id).isDisabled()
  return states
}

async function control(action, extra = {}) {
  const r = await fetch(controlBase + '/control', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ...extra }) })
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(`控制面 ${action} 失败：${JSON.stringify(body)}`)
  return body
}

const near = (a, b, tol = 2) => Math.abs(a - b) <= tol
const rectNear = (pr, rect, tol = 2) =>
  near(pr.left, Math.floor(rect.sx), tol) && near(pr.top, Math.floor(rect.sy), tol)
  && near(pr.right, Math.ceil(rect.ex), tol) && near(pr.bottom, Math.ceil(rect.ey), tol)

function lastApi(match) {
  const keys = Object.keys(apiResponses).filter(k => k.includes(match))
  if (!keys.length) throw new Error(`没有捕获到 ${match} 的正式响应`)
  return apiResponses[keys[keys.length - 1]]
}

// —— 迟到/掉包路由控制（保留浏览器原始头） ——
// 'response'：请求立即真实到达服务端（route.fetch 显式转发原始
//  Sec-Fetch-Site/Origin/Cookie/CSRF 头），响应扣住、release 后迟到送达；
// 'hold'：请求扣住（服务端尚未见），release 后原样继续——迟到时旧上下文已被
//  设备切换关闭，服务端真实拒绝（410 一类迟到失败）；
// 'abort'：掉包。
function makeRouter() {
  const state = { save: null, preview: null }
  const waiting = { save: null, preview: null }
  const release = kind => {
    if (waiting[kind]) { waiting[kind](); waiting[kind] = null }
  }
  const wire = kind => async route => {
    if (state[kind] === 'abort') { await route.abort('connectionreset'); return }
    if (state[kind] === 'response') {
      const headers = await route.request().allHeaders()
      const response = await route.fetch({ headers })
      await new Promise(resolve => { waiting[kind] = resolve })
      await route.fulfill({ response })
      return
    }
    if (state[kind] === 'hold')
      await new Promise(resolve => { waiting[kind] = resolve })
    await route.continue()
  }
  page.route('**/api/phone-search-region/save', wire('save'))
  page.route('**/api/phone-search-region/preview', wire('preview'))
  return { state, release }
}

try {
  if (phase === 'main') {
    await step('load', '页面加载与粗框区初始状态', async () => {
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      await $('#regionCanvas').waitFor({ timeout: 15000 })
      await waitMsgAny(['尚未取源帧', '粗框上下文已重置'], 20000)
      const s = await buttonStates()
      if (s.btnRegionOpen) throw new Error('初始「①取源帧」应可用')
      if (!s.btnRegionPreview || !s.btnRegionSave) throw new Error('初始②③应禁用')
      await shot('a-main-01-initial')
      return s
    }, { fatal: true })

    await step('open-frame', '① 取当前绑定相机源帧并框选（冻结证据帧）', async () => {
      await $('#btnRegionOpen').click()
      const m = await waitMsg('已冻结源帧')
      if (!m.includes('机械/输入次数为 0')) throw new Error(`框选提示缺零动作说明：${m}`)
      const s = await buttonStates()
      if (!s.btnRegionPreview) throw new Error('未画框时②应禁用')
      await shot('a-main-02-frame')
      return { msg: m }
    }, { fatal: true })

    const rect1 = { sx: 195, sy: 115, ex: 445, ey: 365 }
    await step('drag-rect', '真实鼠标拖框（源 195,115-445,365；含 letterbox 反算）', async () => {
      await canvasDrag(rect1)
      await page.waitForTimeout(250)
      const stage = await stageText()
      if (!stage.includes('已画框')) throw new Error(`阶段徽章：${stage}`)
      const s = await buttonStates()
      if (s.btnRegionPreview) throw new Error('画框后②应可用')
      if (!s.btnRegionSave) throw new Error('未预览时③应禁用')
      await shot('a-main-03-drawn')
      return { stage }
    }, { fatal: true })

    await step('preview-verified', '② 预览确认：正式预览响应 verified + 坐标一致', async () => {
      const m = await clickPreview()
      const body = lastApi('POST').body
      if (body.status !== 'verified') throw new Error(`预览状态：${JSON.stringify(body.status)}`)
      if (!rectNear(body.pixel_rect, rect1))
        throw new Error(`pixel_rect=${JSON.stringify(body.pixel_rect)} 与拖框不一致`)
      const s = await buttonStates()
      if (s.btnRegionSave) throw new Error('预览后③应可用')
      return { pixel_rect: body.pixel_rect, preview_id: (body.preview_id || '').slice(0, 8) }
    }, { fatal: true })

    await step('save-v1', '③ 保存并发布：页面显示真实版本 v1 / revision 1', async () => {
      const m = await clickSave(1, 1)
      if (!m.includes('不代表校准已通过')) throw new Error(`保存提示语义异常：${m}`)
      const s = await buttonStates()
      if (!s.btnRegionSave) throw new Error('保存后（预览单次消费）③应禁用')
      await shot('a-main-04-saved-v1')
      return { msg: m }
    })

    await step('binding-visible', '保存后读取当前范围：显示真实生效版本与相机绑定', async () => {
      await page.waitForTimeout(600)
      const t = await bindingText()
      if (!t.includes('当前生效')) throw new Error(`未显示当前生效范围：${t}`)
      if (!t.includes('v1')) throw new Error(`未显示版本 v1：${t}`)
      if (!t.includes('cam-01')) throw new Error(`未显示相机 cam-01：${t}`)
      return { text: t }
    })

    const rect2 = { sx: 190, sy: 110, ex: 450, ey: 370 }
    await step('resave-immediate', 'R3-15-A：保存成功后立即再取帧/改框/再保存 v2', async () => {
      await openFrameDrag(rect2)
      await clickPreview()
      const body = lastApi('POST').body
      if (!rectNear(body.pixel_rect, rect2))
        throw new Error(`第二框 pixel_rect=${JSON.stringify(body.pixel_rect)}`)
      const m = await clickSave(2, 2)
      return { msg: m }
    })

    const router = makeRouter()
    const rect3 = { sx: 185, sy: 105, ex: 455, ey: 375 }
    await step('aba-late-success', 'R4-02-B：A→B→A 迟到成功不覆盖新上下文、不释放新 busy', async () => {
      // A：第三轮预览完成；Fleet 对保存/预览加真实延迟 → 保存成功响应在
      // 设备切换之后迟到送达（请求头/Cookie/CSRF 全程真实，不经替身重发）
      await openFrameDrag(rect3)
      await clickPreview()
      await control('fleet_delay', { save_ms: 4500, preview_ms: 4200 })
      const saveDone = page.waitForResponse(r =>
        r.url().includes('/api/phone-search-region/save'), { timeout: 15000 }).catch(() => null)
      await $('#btnRegionSave').click()
      await page.waitForTimeout(500)
      const s = await buttonStates()
      if (!s.btnRegionOpen || !s.btnRegionPreview || !s.btnRegionSave || !s.btnRegionRefresh)
        throw new Error('保存进行中四个按钮应全部禁用（busy）')
      // A→B→A（切设备释放旧设备 busy 并重读正式记录）
      await $('#deviceId').selectOption('arm-02')
      await waitMsg('设备 arm-02')
      await $('#deviceId').selectOption('arm-01')
      await waitMsg('设备 arm-01')
      // 新操作链：开帧+画框+预览（预览同样被延迟 → 新 busy 在途）。
      // 采样按钮态：迟到保存送达期间若错误释放新 busy，采样里会出现 enabled
      await openFrameDrag(rect3)
      const before = { msg: await msgText(), stage: await stageText() }
      const samples = []
      let samplerStop = false
      await $('#btnRegionPreview').click()   // busy 自点击起同步持有，采样在点击后即有效
      const sampler = (async () => {
        try {
          for (let i = 0; i < 120 && !samplerStop; i++) {
            samples.push({ save: await $('#btnRegionSave').isDisabled(),
                           preview: await $('#btnRegionPreview').isDisabled(),
                           msg: (await msgText()).slice(0, 24) })
            await page.waitForTimeout(80)
          }
        } catch { /* 页面关闭后停止采样 */ }
      })()
      // 迟到的保存成功送达（Fleet 延迟到期；页面已在新 generation 上）
      await saveDone
      await page.waitForTimeout(500)
      const afterLate = await msgText()
      if (afterLate.includes('已保存版本'))
        throw new Error(`迟到成功覆盖了当前提示：${afterLate}`)
      if (before.stage.includes('已画框') && !(await stageText()).includes('已画框'))
        throw new Error('迟到成功覆盖了画框状态')
      await control('fleet_delay', { save_ms: 0, preview_ms: 0 })
      const m = await waitMsg('预览有效')
      samplerStop = true
      await sampler
      const lateSamples = samples.filter(x => x.msg.includes('已冻结') || x.msg.includes('已画框'))
      const stolen = lateSamples.filter(x => !x.save || !x.preview)
      if (stolen.length)
        throw new Error(`迟到成功释放了新操作 busy：${JSON.stringify(stolen.slice(0, 3))}`)
      const s3 = await buttonStates()
      if (s3.btnRegionSave) throw new Error('新预览完成后③应恢复可用')
      const status = await control('status')
      await shot('a-main-05-aba-late-success')
      if (status.region.revision < 3)
        throw new Error(`迟到保存应在服务端生效（v3）：${JSON.stringify(status.region)}`)
      return { lateMsgIgnored: !afterLate.includes('已保存版本'),
               revision: status.region.revision, previewMsg: m.slice(0, 50) }
    })

    const rect5 = { sx: 186, sy: 106, ex: 456, ey: 374 }
    await step('aba-late-failure', 'R4-02-B：迟到失败（服务端真实拒绝：旧上下文失效/暗帧复验）同样不覆盖不释放', async () => {
      await openFrameDrag(rect5)
      await clickPreview()
      // 手机移出（暗帧）：若旧上下文仍有效，保存复验失败（409）；
      // 设备切换已关闭旧上下文时为 410——两者都是服务端真实失败，迟到送达
      await control('push_mode', { mode: 'dark' })
      router.state.save = 'hold'
      await $('#btnRegionSave').click()
      await page.waitForTimeout(500)
      await $('#deviceId').selectOption('arm-02')
      await waitMsg('设备 arm-02')
      await $('#deviceId').selectOption('arm-01')
      await waitMsg('设备 arm-01')
      await openFrameDrag(rect5)
      router.state.preview = 'hold'
      const before = await msgText()
      await $('#btnRegionPreview').click()
      await page.waitForTimeout(300)
      // 迟到的 409 送达
      router.state.save = null
      router.release('save')
      await page.waitForTimeout(400)
      const s2 = await buttonStates()
      if (!s2.btnRegionPreview || !s2.btnRegionSave)
        throw new Error('迟到失败不得释放新操作 busy')
      const afterLate = await msgText()
      if (afterLate.includes('保存失败') && afterLate !== before)
        throw new Error(`迟到失败覆盖了当前提示：${afterLate}`)
      // 恢复亮帧后放行新预览请求（服务端此时检测亮帧）
      await control('push_mode', { mode: 'phone' })
      router.state.preview = null
      router.release('preview')
      const m = await waitMsg('预览有效')
      let lateStatus = null
      try { lateStatus = lastApi('save').status } catch { }
      await shot('a-main-06-aba-late-failure')
      return { lateFailureIgnored: !afterLate.includes('保存失败'),
               lateSaveStatus: lateStatus, previewMsg: m.slice(0, 50) }
    })

    await step('packet-loss-retry', 'R4-02-C/R3-15-B：保存掉包→失败提示→busy 释放→重试成功', async () => {
      const before = await control('status')
      const revisionBefore = before.region.revision
      await openFrameDrag(rect5)
      await clickPreview()
      router.state.save = 'abort'
      await $('#btnRegionSave').click()
      const m = await waitMsg('保存失败')
      const s = await buttonStates()
      if (s.btnRegionOpen || s.btnRegionSave)
        throw new Error('掉包失败后 busy 应释放（①③恢复可用）')
      router.state.save = null
      const afterFail = await control('status')
      if (afterFail.region.revision !== revisionBefore)
        throw new Error('掉包的保存在服务端不应生效')
      await $('#btnRegionSave').click()   // 重试（预览未被消费）
      await waitMsg('已保存版本')
      const finalStatus = await control('status')
      if (finalStatus.region.revision !== revisionBefore + 1)
        throw new Error(`重试后指针应 +1：${finalStatus.region.revision}`)
      await shot('a-main-07-packet-loss-retry')
      return { revisionBefore, revisionAfter: finalStatus.region.revision, failMsg: m.slice(0, 60) }
    })

    await step('zero-motion', 'R4-02-E：全程底层动作计数为零', async () => {
      const status = await control('status')
      if (status.motion_total !== 0)
        throw new Error(`假 daemon 收到运动指令 ${status.motion_total} 条：${status.commands}`)
      return { motion_total: status.motion_total, commands: status.commands.slice(0, 20) }
    })

    await step('formal-requests-recorded', 'R4-02-E：正式请求与契约版本记录', async () => {
      const saves = requests.filter(r => r.url.includes('/api/phone-search-region/save'))
      const previews = requests.filter(r => r.url.includes('/api/phone-search-region/preview'))
      if (!saves.length || !previews.length) throw new Error('未捕获正式保存/预览请求')
      const bodies = Object.values(apiResponses).map(e => e.body)
      const versions = bodies.filter(b => b && b.contract_version)
        .map(b => b.contract_version)
      if (!versions.includes('phone-search-region-write.v1'))
        throw new Error(`未见写契约版本：${versions}`)
      await shot('a-main-05-final')
      return { saves: saves.length, previews: previews.length, versions }
    })
  }

  if (phase === 'letterbox') {
    await step('letterbox-load', '页面加载（letterbox 阶段前置）', async () => {
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      await $('#regionCanvas').waitFor({ timeout: 15000 })
      await waitMsgAny(['尚未取源帧', '粗框上下文已重置'], 20000)
      return {}
    }, { fatal: true })
    const viewports = [[1440, 900], [700, 500], [375, 812]]
    const rectL = { sx: 192, sy: 112, ex: 448, ey: 368 }
    const results = []
    for (const [w, h] of viewports) {
      await step(`viewport-${w}x${h}`, `实际视口 ${w}x${h}（含 letterbox）拖框坐标一致`, async () => {
        await page.setViewportSize({ width: w, height: h })
        await openFrameDrag(rectL)
        const m = await clickPreview()
        const body = lastApi('POST').body
        if (body.status !== 'verified') throw new Error(`预览状态：${body.status}`)
        if (!rectNear(body.pixel_rect, rectL))
          throw new Error(`pixel_rect=${JSON.stringify(body.pixel_rect)} 偏移超差`)
        const s = await buttonStates()
        if (s.btnRegionPreview || s.btnRegionSave) throw new Error('窄屏按钮禁用状态不正确')
        results.push(body.pixel_rect)
        await shot(`a-letterbox-${w}x${h}`)
        return { pixel_rect: body.pixel_rect, msg: m.slice(0, 60) }
      })
    }
    await step('three-places-consistent', 'R4-02-D：桌面/中屏/窄屏三处坐标一致', () => {
      const [a, b, c] = results
      for (const [key, value] of Object.entries(a)) {
        if (Math.abs(value - b[key]) > 2 || Math.abs(value - c[key]) > 2)
          throw new Error(`三视口 ${key} 不一致：${JSON.stringify(results)}`)
      }
      return results
    })
  }

  if (phase === 'camera-switch') {
    await step('switch-and-refresh', '换相机后旧资格立即可见失效（不静默沿用）', async () => {
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      await $('#regionCanvas').waitFor({ timeout: 15000 })
      await waitMsgAny(['尚未取源帧', '粗框上下文已重置'], 20000)
      await control('switch_camera', { device: 'arm-01' })
      // 旧上下文下读取当前范围：被相机身份变化拒绝（页面可见失败，不静默）
      await $('#btnRegionRefresh').click()
      await waitMsgRe(/读取失败.*camera_(unique_id|binding)_?changed?|读取失败.*camera/, 8000)
      const m1 = await msgText()
      if (!/camera_unique_id_changed|camera_binding_revision_changed/.test(m1))
        throw new Error(`旧相机资格应被拒，页面提示：${m1}`)
      await shot('a-camswitch-01-stale')
      return { msg: m1 }
    })

    await step('old-context-rejected', '旧上下文取源帧被拒（资格失效不静默放行）', async () => {
      await $('#btnRegionOpen').click()
      const m = await waitMsgRe(/源帧获取失败/, 8000)
      if (!/camera_unique_id_changed|camera_binding_revision_changed|camera_binding_mismatch/.test(m))
        throw new Error(`旧相机资格应被拒，页面提示：${m}`)
      return { msg: m }
    })

    await step('requify-and-resave', '重开页面（新绑定上下文）重新框选并保存新版本', async () => {
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      await $('#regionCanvas').waitFor({ timeout: 15000 })
      await waitMsgAny(['尚未取源帧', '粗框上下文已重置'], 20000)
      // 等初始正式读取完成（画框会作废在途读取，保存前必须拿到真实 revision）
      await page.waitForTimeout(1200)
      const t0 = await bindingText()
      if (!/失效|绑定变化/.test(t0))
        throw new Error(`换相机后重载应显示旧范围失效：${t0}`)
      await openFrameDrag({ sx: 192, sy: 112, ex: 448, ey: 368 })
      await clickPreview()
      const status = await control('status')
      const version = status.region.selected ? status.region.selected.version : 0
      let m = ''
      try {
        m = await clickSave(version + 1, version + 1)
      } catch (e) {
        // 初读若仍被作废：CAS 冲突后页面已重读 revision，人工再点一次必须成功
        m = await clickSave(version + 1, version + 1)
      }
      await page.waitForTimeout(800)
      const t = await bindingText()
      if (!t.includes(`v${version + 1}`)) throw new Error(`新绑定保存后未显示 v${version + 1}：${t}`)
      const status2 = await control('status')
      if (status2.region.status !== 'verified')
        throw new Error(`新绑定下范围应为 verified：${status2.region.status}`)
      await shot('a-camswitch-02-resaved')
      return { msg: m.slice(0, 60), binding: t, regionStatus: status2.region.status }
    })

    await step('zero-motion-camswitch', '换相机全程零动作', async () => {
      const status = await control('status')
      if (status.motion_total !== 0)
        throw new Error(`运动指令 ${status.motion_total} 条：${status.commands}`)
      return { motion_total: status.motion_total }
    })
  }

  if (phase === 'restart') {
    await step('reload-after-restart', '同数据根重启后重载：正式记录与重启前一致', async () => {
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      await $('#regionCanvas').waitFor({ timeout: 15000 })
      await waitMsgAny(['尚未取源帧', '粗框上下文已重置'], 20000)
      await $('#btnRegionRefresh').click()
      await page.waitForTimeout(1500)
      const t = await bindingText()
      if (!t.includes('当前生效')) throw new Error(`重启后未显示当前生效范围：${t}`)
      const status = await control('status')
      if (status.region.status !== 'verified') throw new Error(`状态：${status.region.status}`)
      const m = await msgText()
      await shot('a-restart-01-reread')
      return { binding: t, region: status.region, msg: m }
    })
  }

  if (phase === 'h01' || phase === 'h01-restart') {
    await step('h01-warnings-visible', 'H01：过程/终局页面显示采样姿态告警（仅提示不中断）', async () => {
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      const box = $('#wzAutoPoseWarnings')
      await box.waitFor({ state: 'visible', timeout: 20000 })
      const text = (await box.textContent()) || ''
      if (!text.includes('采样姿态提示')) throw new Error(`告警框标题缺失：${text}`)
      if (!text.includes('仅告警不中断')) throw new Error(`未声明仅告警：${text}`)
      if (!text.includes('采样姿态超限提示')) throw new Error(`缺真实超限条目：${text}`)
      if (!text.includes('6.1px')) throw new Error(`缺可测得位移 6.1px：${text}`)
      if (!text.includes('采样姿态未核实提示')) throw new Error(`缺无法测得条目：${text}`)
      if (!text.includes('姿态未核实')) throw new Error(`缺姿态未核实文案：${text}`)
      // 混写检查：超限条目不得显示“姿态未核实”，未核实条目不得显示 px 数值位移
      const lines = text.split('\n').filter(l => l.includes('采样姿态超限提示'))
      if (lines.some(l => l.includes('姿态未核实'))) throw new Error(`超限与未核实混写：${text}`)
      const unver = text.split('\n').filter(l => l.includes('采样姿态未核实提示'))
      if (unver.some(l => /位移 \d+(\.\d+)?px/.test(l))) throw new Error(`未核实条目编造位移：${text}`)
      await shot('a-h01-01-warnings')
      return { text }
    }, { fatal: true })

    await step('h01-run-kept', 'H01：run 记录保留（不作废、不强制重做）', async () => {
      // 经正式 /api/wizard/state（控制面以页面同款上下文/CSRF 调用）
      const summary = await control('wizard_state', { device: 'arm-01' })
      const auto = summary.auto || {}
      if (!auto.runId && !auto.lastRun) throw new Error('向导状态无 run 记录')
      const source = auto.lastRun || auto
      if (!source.poseWarnings || source.poseWarnings.length < 2)
        throw new Error(`poseWarnings 应≥2：${JSON.stringify(source.poseWarnings)}`)
      if (auto.lastFailure && auto.lastFailure.code === 'pose_sampling_warning')
        throw new Error('采样姿态告警被当成失败')
      const visible = (await $('#wzAutoPoseWarnings').isVisible())
        && !(await $('#wzAutoFailure').isVisible())
      return { runId: source.runId, warnings: source.poseWarnings.length,
               failureVisible: await $('#wzAutoFailure').isVisible(), visible }
    })
  }
} catch (error) {
  cases.push({ case_id: 'fatal', title: phase, status: 'failed',
               error: String(error && error.message || error) })
} finally {
  await writeOut()
}
async function writeOut() {
  const passed = cases.filter(c => c.status === 'passed').length
  const failed = cases.length - passed
  const report = {
    schema: 'browser-json-v1', phase, base, control: controlBase,
    generated_at: new Date().toISOString(),
    cases, requests,
    api_responses: apiResponses,
    summary: { total: cases.length, passed, failed },
  }
  writeFileSync(outJson, JSON.stringify(report, null, 2))
}
process.exitCode = softFailures > 0 || cases.some(c => c.status === 'failed') ? 1 : 0
await browser.close()
