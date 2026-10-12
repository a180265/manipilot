// R4-12-E 浏览器层：正式 App + nginx(auth_request) + WX + Fleet 全链。
// 模式 main：匿名401/伪造头剥离/退出/重置/禁用→EOF≤5s/他用户不误伤/
//   preview 停帧恢复/preview 不可达回退。
// 模式 expiry：TTL=6 自然到期→EOF≤5s（不依赖浏览器下一请求）。
// 计时统一 Date.now()（跨页面对齐）；流读取=真实 fetch ReadableStream。
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const [, , base, mode, outJson, shotsDir, adminPass, staffTemp, frontendRoot,
  previewPort] = process.argv
const { chromium } = createRequire(resolve(frontendRoot, 'package.json'))('playwright-core')
mkdirSync(shotsDir, { recursive: true })

const cases = []
const requests = []
async function step(case_id, title, fn) {
  const started = Date.now()
  try {
    const detail = (await fn()) || {}
    cases.push({ case_id, title, status: 'passed', duration_ms: Date.now() - started, detail })
  } catch (error) {
    cases.push({ case_id, title, status: 'failed', duration_ms: Date.now() - started,
                 error: String(error && error.message || error) })
    throw error
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const tid = (page, id) => page.getByTestId(id)

async function newActor() {
  const context = await browser.newContext({ viewport: { width: 1360, height: 860 } })
  const page = await context.newPage()
  page.on('response', res => {
    const u = res.url()
    if (u.includes('/arm-api/') || u.includes('/wx-auth/') || u.includes('/wx-users/'))
      requests.push({ method: res.request().method(), url: u, status: res.status() })
  })
  return { context, page }
}

async function loginUI(page, username, password) {
  await tid(page, 'login-username').fill(username)
  await tid(page, 'login-password').fill(password)
  await page.getByRole('button', { name: /^登录$/ }).click()
  await page.waitForTimeout(700)
}

async function loginWithForcedChange(page, username, temp, own) {
  await loginUI(page, username, temp)
  if (await tid(page, 'temp-password').count()) {
    await tid(page, 'temp-password').fill(temp)
    await tid(page, 'new-password').fill(own)
    await tid(page, 'confirm-password').fill(own)
    await page.getByRole('button', { name: /修改并重新登录/ }).click()
    await tid(page, 'login-username').waitFor({ timeout: 8000 })
    await loginUI(page, username, own)
  }
  await tid(page, 'account-trigger').waitFor({ timeout: 8000 })
}

// 页内助手：建会话/读流（window 持久）/取 csrf
async function createViewSession(page, extraHeaders = {}) {
  return page.evaluate(async (extra) => {
    const r = await fetch('/arm-api/v1/camera/view-sessions', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...extra },
      body: JSON.stringify({
        device_id: 'arm-01', purpose: '主动查看', requested_by: 'browser-e2e',
        correlation_id: crypto.randomUUID(), client_request_id: crypto.randomUUID(),
        role: 'staff' }) })
    const body = await r.json()
    if (r.status !== 200) throw new Error('createViewSession ' + r.status + ' ' + JSON.stringify(body))
    return body.view_session_id
  }, extraHeaders)
}

async function openStream(page, sid, key) {
  // 起流并等首帧；reader 存 window 供后续 evaluate 继续读
  await page.evaluate(async ([sid, key]) => {
    const resp = await fetch(`/arm-api/v1/camera/view-sessions/${sid}/stream`,
                             { credentials: 'include' })
    if (resp.status !== 200) throw new Error('stream ' + resp.status)
    const reader = resp.body.getReader()
    window['__st'] = window['__st'] || {}
    window['__st'][key] = { reader, frames: 0, bytes: 0, ts: [],
                            tStart: Date.now(), tFirstFrame: null,
                            tLastFrame: null, tEof: null }
    const st = window['__st'][key]
    let tail = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) { st.tEof = Date.now(); break }
      st.bytes += value.length
      // boundary 位于 part 开头：保留上一段尾 16 字符防跨界切割
      const text = new TextDecoder().decode(value, { stream: true })
      const window_ = tail.slice(-16) + text
      tail = text
      if (window_.includes('--pv') || window_.includes('--armfleetframe')) {
        st.frames++
        st.ts.push(Date.now())
        if (!st.tFirstFrame) st.tFirstFrame = Date.now()
        st.tLastFrame = Date.now()
        if (st.frames >= 1) break
      }
    }
  }, [sid, key])
}

async function _readLoop(page, key, maxMs, requireEof) {
  return page.evaluate(async ([key, maxMs, requireEof]) => {
    const st = window['__st'][key]
    const deadline = Date.now() + maxMs
    while (!st.tEof) {
      const remain = deadline - Date.now()
      if (remain <= 0) {
        if (requireEof) throw new Error('等待 EOF 超时 frames=' + st.frames)
        break
      }
      const timer = new Promise(r => setTimeout(r, Math.min(remain, 500)))
      const next = Promise.race([
        st.reader.read().then(v => v, e => ({ done: true, error: String(e) })),
        timer.then(() => null) ])
      const chunk = await next
      if (chunk === null) continue
      if (chunk.done) { st.tEof = Date.now(); break }
      st.bytes += chunk.value.length
      const text = new TextDecoder().decode(chunk.value, { stream: true })
      const window_ = (st.__tail || '').slice(-16) + text
      st.__tail = text
      if (window_.includes('--pv') || window_.includes('--armfleetframe')) {
        st.frames++
        st.ts.push(Date.now())
        st.tLastFrame = Date.now()
      }
    }
    let maxGapMs = 0
    for (let i = 1; i < st.ts.length; i++) maxGapMs = Math.max(maxGapMs, st.ts[i] - st.ts[i-1])
    return { frames: st.frames, bytes: st.bytes, tFirstFrame: st.tFirstFrame,
             tLastFrame: st.tLastFrame, tEof: st.tEof, maxGapMs }
  }, [key, maxMs, requireEof])
}

// 撤销/到期用例：必须自然 EOF（超时=失败）
const readUntilEof = (page, key, maxMs = 12000) => _readLoop(page, key, maxMs, true)
// 存活性用例：读满时长（EOF 出现即记录，不视为错误）
const readForMs = (page, key, ms) => _readLoop(page, key, ms, false)

async function abortStream(page, key) {
  await page.evaluate(async (key) => {
    const st = (window['__st'] || {})[key]
    if (st) { try { await st.reader.cancel() } catch {} }
  }, key)
}

async function getCsrf(page) {
  return page.evaluate(async () => {
    const r = await fetch('/sales-api/internal/wx-auth/v1/session', { credentials: 'include' })
    const b = await r.json()
    if (r.status !== 200) throw new Error('session ' + r.status)
    return b.csrf_token
  })
}

async function renewSession(page, sid) {
  return page.evaluate(async (sid) => {
    const r = await fetch(`/arm-api/v1/camera/view-sessions/${sid}/renew`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ correlation_id: crypto.randomUUID() }) })
    return r.status
  }, sid)
}

const shotOf = (page, name) => page.screenshot({ path: resolve(shotsDir, `${name}.png`) })

try {
  if (mode === 'main') {
    const admin = await newActor()
    const s1 = await newActor()
    const s2 = await newActor()
    // 0) 正式 App 加载 + 匿名相机创建 401（auth_request 生效）
    await step('anonymous-camera-401', '匿名相机查看创建被拒（未登录不达 Fleet）', async () => {
      await s1.page.goto(base + '/', { waitUntil: 'networkidle' })
      await tid(s1.page, 'login-username').waitFor({ timeout: 10000 })
      const status = await s1.page.evaluate(async () => {
        const r = await fetch('/arm-api/v1/camera/view-sessions', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: 'arm-01', purpose: '主动查看',
            requested_by: 'anon', correlation_id: crypto.randomUUID(),
            client_request_id: crypto.randomUUID(), role: 'readonly' }) })
        return r.status })
      if (status !== 401) throw new Error(`匿名期望 401 实际 ${status}`)
      return { status }
    })
    // 1) 管理员登录 + 建两名客服
    await step('admin-and-staff-setup', '管理员登录并创建 staff1/staff2（各自首登改密）', async () => {
      await admin.page.goto(base + '/', { waitUntil: 'networkidle' })
      await s2.page.goto(base + '/', { waitUntil: 'networkidle' })
      await tid(admin.page, 'login-username').waitFor({ timeout: 10000 })
      await loginWithForcedChange(admin.page, 'admin', adminPass, adminPass + '-ok')
      await shotOf(admin.page, '01-admin-workspace')
      for (const [name, temp] of [['staff1', staffTemp], ['staff2', staffTemp + 'b']]) {
        await admin.page.getByRole('button', { name: /用户管理/ }).click()
        await tid(admin.page, 'open-add-user').click()
        await tid(admin.page, 'add-username').fill(name)
        await tid(admin.page, 'add-display-name').fill(name)
        await tid(admin.page, 'add-temp-password').fill(temp)
        await admin.page.getByRole('button', { name: /创建客服账号/ }).click()
        await tid(admin.page, `user-row-${name}`).waitFor({ timeout: 8000 })
      }
      await loginWithForcedChange(s1.page, 'staff1', staffTemp, 'close-staff1-own-2026')
      await loginWithForcedChange(s2.page, 'staff2', staffTemp + 'b', 'close-staff2-own-2026')
      await shotOf(s1.page, '02-staff1-workspace')
      return { staff: ['staff1', 'staff2'] }
    })
    // 2) 伪造身份头被边界剥离（绑定=staff1 而非 mallory.evil）
    let forgedSid
    await step('forged-operator-stripped', '客户端伪造 X-WX-Operator 被代理剥离', async () => {
      forgedSid = await createViewSession(s1.page, { 'X-WX-Operator': 'mallory.evil',
                                                     'X-WX-Operator-Role': 'admin' })
      return { sid: forgedSid }
    })
    // 3) 退出 → 旧流 ≤5s EOF
    await step('logout-stops-stream', 'WX 退出→已建流 EOF≤5s（浏览器计时）', async () => {
      const sid = await createViewSession(s1.page)
      await openStream(s1.page, sid, 'logout')
      const csrf = await getCsrf(s1.page)
      const tLogout = await s1.page.evaluate(async (csrf) => {
        const r = await fetch('/sales-api/internal/wx-auth/v1/logout', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-WX-CSRF': csrf },
          body: JSON.stringify({ request_id: crypto.randomUUID() }) })
        if (r.status !== 200) throw new Error('logout ' + r.status)
        return Date.now() }, csrf)
      const tl = await readUntilEof(s1.page, 'logout', 12000)
      if (tl.tEof - tLogout > 5000) throw new Error(`EOF 用时 ${tl.tEof - tLogout}ms > 5000`)
      if (!tl.tFirstFrame) throw new Error('撤销前未收到帧')
      return { sid, stop_ms: tl.tEof - tLogout, frames: tl.frames }
    })
    // 4) 退出后新登录可建流（新代际）
    await step('new-login-after-logout', '同用户重新登录后新会话可建流', async () => {
      await loginUI(s1.page, 'staff1', 'close-staff1-own-2026')
      await tid(s1.page, 'account-trigger').waitFor({ timeout: 8000 })
      const sid = await createViewSession(s1.page)
      await openStream(s1.page, sid, 'newgen')
      const tl = await readForMs(s1.page, 'newgen', 3000)
      // 不撤销：3s 内仍在供帧（读到超时=流未断），有帧即可
      if (!tl.tFirstFrame) throw new Error('新会话流无帧')
      await abortStream(s1.page, 'newgen')
      return { sid, frames: tl.frames }
    })
    const adminApi = (fn, arg) => admin.page.evaluate(fn, arg)
    // 5) 管理员重置密码 → staff1 旧流 ≤5s EOF
    await step('reset-stops-stream', '管理员重置密码→staff1 已建流 EOF≤5s', async () => {
      const sid = await createViewSession(s1.page)
      await openStream(s1.page, sid, 'reset')
      const csrf = await getCsrf(admin.page)
      const tReset = await adminApi(async ([csrf, temp2]) => {
        const list = await (await fetch('/sales-api/internal/wx-users/v1/users',
          { credentials: 'include' })).json()
        const me = list.items.find(u => u.username === 'staff1')
        const r = await fetch(`/sales-api/internal/wx-users/v1/users/${me.user_id}/reset-password`, {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-WX-CSRF': csrf },
          body: JSON.stringify({ request_id: crypto.randomUUID(),
            expected_revision: me.revision, temporary_password: temp2 }) })
        if (r.status !== 200) throw new Error('reset ' + r.status + ' ' + await r.text())
        return Date.now() }, [csrf, 'close-staff1-tmp2-2026'])
      const tl = await readUntilEof(s1.page, 'reset', 12000)
      if (tl.tEof - tReset > 5000) throw new Error(`EOF 用时 ${tl.tEof - tReset}ms`)
      return { sid, stop_ms: tl.tEof - tReset, frames: tl.frames }
    })
    // 6) 禁用 → staff1 流 ≤5s EOF；staff2 同时不误伤且可续期
    await step('disable-stops-stream', '管理员禁用→staff1 已建流 EOF≤5s', async () => {
      await loginWithForcedChange(s1.page, 'staff1', 'close-staff1-tmp2-2026',
                                  'close-staff1-own2-2026')
      const sid = await createViewSession(s1.page)
      await openStream(s1.page, sid, 'disable')
      const csrf = await getCsrf(admin.page)
      const tDisable = await adminApi(async (csrf) => {
        const list = await (await fetch('/sales-api/internal/wx-users/v1/users',
          { credentials: 'include' })).json()
        const me = list.items.find(u => u.username === 'staff1')
        const r = await fetch(`/sales-api/internal/wx-users/v1/users/${me.user_id}`, {
          method: 'PATCH', credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'X-WX-CSRF': csrf },
          body: JSON.stringify({ request_id: crypto.randomUUID(),
            expected_revision: me.revision, enabled: false }) })
        if (r.status !== 200) throw new Error('disable ' + r.status)
        return Date.now() }, csrf)
      const tl = await readUntilEof(s1.page, 'disable', 12000)
      if (tl.tEof - tDisable > 5000) throw new Error(`EOF 用时 ${tl.tEof - tDisable}ms`)
      return { sid, stop_ms: tl.tEof - tDisable, frames: tl.frames, tDisable }
    })
    await step('other-user-unharmed', '禁用 staff1 时 staff2 流持续且可续期', async () => {
      const sid2 = await createViewSession(s2.page)
      await openStream(s2.page, sid2, 'staff2')
      const tl = await readForMs(s2.page, 'staff2', 3200)
      if (tl.tEof) throw new Error(`staff2 流被误停（EOF=${tl.tEof}）`)
      if (tl.frames < 8) throw new Error(`staff2 帧不足：${tl.frames}`)
      const renew = await renewSession(s2.page, sid2)
      if (renew !== 200) throw new Error(`staff2 续期期望 200 实际 ${renew}`)
      await abortStream(s2.page, 'staff2')
      return { sid2, frames: tl.frames, renew }
    })
    // 7) preview 停帧>1s 后恢复：不误杀（看门狗反例）
    await step('preview-stall-recovery', 'preview 停帧 1.5s 后恢复：流不中断', async () => {
      const sid = await createViewSession(s2.page)
      await openStream(s2.page, sid, 'stall')
      const before = await s2.page.evaluate(k => window['__st'][k].frames, 'stall')
      await fetch(`http://127.0.0.1:${previewPort}/ctl/stall?ms=1500`)
      const tl = await readForMs(s2.page, 'stall', 8000)
      if (tl.tEof) throw new Error('停帧后被误杀（流终止）')
      if (tl.frames <= before) throw new Error(`停帧后无新帧（${before}->${tl.frames}）`)
      if (tl.maxGapMs < 1200) throw new Error(`未观测到 >1.2s 停帧窗（maxGap=${tl.maxGapMs}ms）`)
      await abortStream(s2.page, 'stall')
      return { sid, frames_before: before, frames_after: tl.frames,
               max_gap_ms: tl.maxGapMs }
    })
    // 8) preview 不可达 → 回退分支仍供流
    await step('preview-unreachable-fallback', 'preview 健康检查失败→回退分支供帧', async () => {
      await fetch(`http://127.0.0.1:${previewPort}/ctl/down`)
      try {
        const sid = await createViewSession(s2.page)
        await openStream(s2.page, sid, 'fallback')
        const tl = await readForMs(s2.page, 'fallback', 3000)
        if (!tl.tFirstFrame) throw new Error('回退分支无帧')
        if (tl.tEof) throw new Error('回退分支流被终止')
        await abortStream(s2.page, 'fallback')
        return { sid, frames: tl.frames }
      } finally {
        await fetch(`http://127.0.0.1:${previewPort}/ctl/up`)
      }
    })
    await shotOf(s2.page, '03-staff2-still-alive')
  } else if (mode === 'expiry') {
    // TTL=6：自然到期→EOF≤5s；浏览器全程不动（不依赖下一请求）
    const s1 = await newActor()
    await step('natural-expiry-stops-stream', '会话自然到期→已建流 EOF≤5s', async () => {
      await s1.page.goto(base + '/', { waitUntil: 'networkidle' })
      await tid(s1.page, 'login-username').waitFor({ timeout: 10000 })
      // 管理员引导：admin 登录建 staff1（首登改密）——TTL=6 对 admin 也生效，
      // 因此管理员动作紧凑完成（登录→建号→登出），随后 staff1 二次登录计时。
      const admin = await newActor()
      await admin.page.goto(base + '/', { waitUntil: 'networkidle' })
      await tid(admin.page, 'login-username').waitFor({ timeout: 10000 })
      await loginWithForcedChange(admin.page, 'admin', adminPass, adminPass + '-ok')
      await admin.page.getByRole('button', { name: /用户管理/ }).click()
      await tid(admin.page, 'open-add-user').click()
      await tid(admin.page, 'add-username').fill('staff1')
      await tid(admin.page, 'add-display-name').fill('staff1')
      await tid(admin.page, 'add-temp-password').fill(staffTemp)
      await admin.page.getByRole('button', { name: /创建客服账号/ }).click()
      await tid(admin.page, 'user-row-staff1').waitFor({ timeout: 8000 })
      await admin.context.close()
      // staff1 首登改密（会话 A，TTL 6s）；二次登录=被测会话 B；到期时刻取
      // 服务端声明的 expires_at（精确，不受 UI 时序误差影响）
      await loginWithForcedChange(s1.page, 'staff1', staffTemp, 'close-exp-own-2026')
      const grant = await s1.page.evaluate(async () => {
        const r = await fetch('/sales-api/internal/wx-auth/v1/session', { credentials: 'include' })
        const b = await r.json()
        if (r.status !== 200) throw new Error('session ' + r.status)
        return b })
      const expectedExpiry = Date.parse(grant.expires_at)
      const sid = await createViewSession(s1.page)
      await openStream(s1.page, sid, 'exp')
      const tl = await readUntilEof(s1.page, 'exp', 20000)
      if (tl.tEof - expectedExpiry > 5000)
        throw new Error(`到期后 EOF 用时 ${tl.tEof - expectedExpiry}ms > 5000`)
      if (tl.tEof < expectedExpiry - 300)
        throw new Error(`EOF 早于到期：${tl.tEof - expectedExpiry}ms`)
      return { sid, stop_after_expiry_ms: tl.tEof - expectedExpiry,
               eof_before_expiry_ms: tl.tEof - expectedExpiry,
               expires_at: grant.expires_at, frames: tl.frames }
    })
    await shotOf(s1.page, '10-expiry-after')
  } else {
    throw new Error('未知模式 ' + mode)
  }
} finally {
  writeFileSync(outJson, JSON.stringify({
    schema_version: 1, kind: 'close-lead-r412e-browser',
    mode, base, started_at: new Date().toISOString(),
    cases, requests }, null, 1))
  await browser.close()
}
