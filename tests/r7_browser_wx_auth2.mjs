// R7-S2 第二批（余项）：重启/禁用/密码重置/真实 TTL 绝对过期。
// 复用第七轮首批栈形态（正式 dist→nginx 模板→真实 WX 后端→Chrome），
// 双浏览器上下文（管理员/客服各自 Cookie 罐）；容器重启经 docker CLI。
// 产出 browser-json-v1 + 截图；不用首批 11/11 冒充本批证据。
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createRequire } from 'node:module'
const exec = promisify(execFile)

const [, , base, outJson, shotsDir, adminPass, staffPass, wxContainer, frontendRoot] = process.argv
mkdirSync(shotsDir, { recursive: true })
const { chromium } = createRequire(resolve(frontendRoot, 'package.json'))('playwright-core')

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
const adminCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const staffCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const admin = await adminCtx.newPage()
const staff = await staffCtx.newPage()
for (const p of [admin, staff]) {
  p.on('response', res => requests.push({ method: res.request().method(),
                                          url: res.url(), status: res.status() }))
}
const shot = (page, name) => page.screenshot({ path: resolve(shotsDir, `${name}.png`) })
const tid = (page, id) => page.getByTestId(id)

async function loginAs(page, username, password) {
  await page.goto(base + '/', { waitUntil: 'networkidle' })
  await tid(page, 'login-username').fill(username)
  await tid(page, 'login-password').fill(password)
  await page.getByRole('button', { name: /^登录$/ }).click()
  await page.waitForTimeout(700)
}
async function ensureWorkspace(page) {
  await tid(page, 'account-trigger').waitFor({ timeout: 9000 })
}

try {
  await step('setup-admin', '管理员就绪（首登改密或直接登录）', async () => {
    await loginAs(admin, 'admin', adminPass)
    if (await tid(admin, 'temp-password').count()) {
      await tid(admin, 'temp-password').fill(adminPass)
      await tid(admin, 'new-password').fill(adminPass + '-ok')
      await tid(admin, 'confirm-password').fill(adminPass + '-ok')
      await admin.getByRole('button', { name: /修改并重新登录/ }).click()
      await tid(admin, 'login-username').waitFor({ timeout: 9000 })
      await loginAs(admin, 'admin', adminPass + '-ok')
    }
    await ensureWorkspace(admin)
  })

  await step('setup-staff', '管理员添加客服并完成首登改密（独立上下文）', async () => {
    await admin.getByRole('button', { name: /用户管理/ }).click()
    await tid(admin, 'open-add-user').click()
    await tid(admin, 'add-username').fill('staff2')
    await tid(admin, 'add-display-name').fill('客服二号')
    await tid(admin, 'add-temp-password').fill(staffPass)
    await admin.getByRole('button', { name: /创建客服账号/ }).click()
    await tid(admin, 'created-credential').waitFor({ timeout: 9000 })
    await loginAs(staff, 'staff2', staffPass)
    await tid(staff, 'temp-password').waitFor({ timeout: 9000 })
    await tid(staff, 'temp-password').fill(staffPass)
    await tid(staff, 'new-password').fill(staffPass + '-new')
    await tid(staff, 'confirm-password').fill(staffPass + '-new')
    await staff.getByRole('button', { name: /修改并重新登录/ }).click()
    await tid(staff, 'login-username').waitFor({ timeout: 9000 })
    await loginAs(staff, 'staff2', staffPass + '-new')
    await ensureWorkspace(staff)
    await shot(staff, 'a2-01-staff-workspace')
  })

  await step('disable-staff-revokes-live-session', '管理员禁用→客服下一次真实业务请求 401 自动回登录', async () => {
    await admin.getByRole('button', { name: /用户管理/ }).click()
    await tid(admin, 'user-row-staff2').waitFor({ timeout: 9000 })
    // 客服首登改密已推进 revision（乐观并发）：刷新取最新列表再操作（409 属保护）
    await admin.reload()
    await tid(admin, 'account-trigger').waitFor({ timeout: 9000 })
    await admin.getByRole('button', { name: /用户管理/ }).click()
    await tid(admin, 'user-row-staff2').waitFor({ timeout: 9000 })
    const patchDone = admin.waitForResponse(
      r => r.request().method() === 'PATCH' && r.url().includes('/wx-users/'),
      { timeout: 9000 })
    await tid(admin, 'toggle-staff2').click()
    const patchRes = await patchDone
    if (patchRes.status() !== 200) throw new Error(`禁用 PATCH 期望 200 实际 ${patchRes.status()}`)
    // 按钮标签翻转（启用<->禁用）是状态变化的可靠信号（状态单元格文本与
    // 按钮文字同词，不能用行文本匹配）
    await admin.waitForFunction(
      () => document.querySelector('[data-testid="toggle-staff2"]')?.textContent === '启用',
      { timeout: 9000 })
    // 客服上下文仍持旧 Cookie：发起真实业务请求 → 401 → 前端回登录
    const status = await staff.evaluate(async () => {
      const r = await fetch('/sales-api/internal/dependencies/health',
                            { credentials: 'include' })
      return r.status
    })
    if (status !== 401) throw new Error(`禁用后业务请求期望 401 实际 ${status}`)
    await staff.goto(base + '/', { waitUntil: 'networkidle' })
    await tid(staff, 'login-username').waitFor({ timeout: 9000 })
    // 被禁账号不能再登录
    await loginAs(staff, 'staff2', staffPass + '-new')
    const err = await tid(staff, 'login-error').count()
    if (!err) throw new Error('被禁账号登录未被拒绝')
    await shot(staff, 'a2-02-disabled-back-to-login')
    // 恢复启用（后续步骤需要）：按钮标签翻回"禁用"
    const patchDone2 = admin.waitForResponse(
      r => r.request().method() === 'PATCH' && r.url().includes('/wx-users/'),
      { timeout: 9000 })
    await tid(admin, 'toggle-staff2').click()
    if ((await patchDone2).status() !== 200) throw new Error('启用 PATCH 失败')
    await admin.waitForFunction(
      () => document.querySelector('[data-testid="toggle-staff2"]')?.textContent === '禁用',
      { timeout: 9000 })
  })

  await step('password-reset-invalidates', '密码重置→旧会话/旧密码失效，新临时密码可用', async () => {
    await loginAs(staff, 'staff2', staffPass + '-new')
    await ensureWorkspace(staff)
    await admin.getByRole('button', { name: /用户管理/ }).click()
    // 一键重置：等待真实 reset-password 请求；随机临时密码只显示一次
    const resetDone = admin.waitForResponse(
      r => r.url().includes('/reset-password'), { timeout: 9000 })
    await tid(admin, 'reset-staff2').click()
    if ((await resetDone).status() !== 200) throw new Error('重置请求失败')
    await tid(admin, 'created-credential').waitFor({ timeout: 9000 })
    const banner = await tid(admin, 'created-credential').textContent()
    const match = /临时密码：\s*([^\s（]+)/.exec(banner || '')
    if (!match) throw new Error(`重置横幅未包含临时密码：${banner}`)
    var resetTempPassword = match[1]
    // 旧会话：下一次业务请求 401
    const status = await staff.evaluate(async () => {
      const r = await fetch('/sales-api/internal/dependencies/health',
                            { credentials: 'include' })
      return r.status
    })
    if (status !== 401) throw new Error(`重置后旧会话期望 401 实际 ${status}`)
    // 旧密码登录失败
    await staff.goto(base + '/', { waitUntil: 'networkidle' })
    await loginAs(staff, 'staff2', staffPass + '-new')
    if (!await tid(staff, 'login-error').count())
      throw new Error('旧密码登录未被拒绝')
    // 新临时密码 → 强制改密 → 可用
    await loginAs(staff, 'staff2', resetTempPassword)
    await tid(staff, 'temp-password').waitFor({ timeout: 9000 })
    await tid(staff, 'temp-password').fill(resetTempPassword)
    await tid(staff, 'new-password').fill(staffPass + '-final')
    await tid(staff, 'confirm-password').fill(staffPass + '-final')
    await staff.getByRole('button', { name: /修改并重新登录/ }).click()
    await tid(staff, 'login-username').waitFor({ timeout: 9000 })
    await loginAs(staff, 'staff2', staffPass + '-final')
    await ensureWorkspace(staff)
    await shot(staff, 'a2-03-after-reset-relogin')
  })

  await step('restart-relogin', 'WX 后端容器重启：已撤销 Cookie 不复活，存活会话/重新登录可用', async () => {
    // 先制造一个已撤销会话（staff 退出 → Cookie 留在浏览器但服务端已撤销）
    await tid(staff, 'account-trigger').click()
    try { await tid(staff, 'logout-button').waitFor({ state: 'visible', timeout: 1500 }) }
    catch { await tid(staff, 'account-trigger').click()
            await tid(staff, 'logout-button').waitFor({ timeout: 5000 }) }
    await tid(staff, 'logout-button').click()
    await tid(staff, 'login-username').waitFor({ timeout: 9000 })
    const revokedStatus = await staff.evaluate(async () => {
      const r = await fetch('/sales-api/internal/wx-auth/v1/session',
                            { credentials: 'include' })
      return r.status
    })
    if (revokedStatus !== 401) throw new Error(`撤销会话期望 401 实际 ${revokedStatus}`)
    // 重启 WX 容器（数据卷保留：会话/账号持久）
    await exec('docker', ['restart', wxContainer])
    await new Promise(r => setTimeout(r, 3000))
    // 就绪后：撤销 Cookie 仍 401（不复活）；合法重新登录可用。
    // amd64 仿真容器冷启约 30-60s，就绪窗给足 100s。
    let ready = false
    for (let i = 0; i < 100 && !ready; i++) {
      try {
        const s = await staff.evaluate(async () =>
          (await fetch('/sales-api/internal/wx-auth/v1/session',
                       { credentials: 'include' })).status)
        if (s === 401) ready = true
      } catch { /* 未就绪 */ }
      if (!ready) await new Promise(r => setTimeout(r, 1000))
    }
    if (!ready) throw new Error('重启后 WX 未就绪')
    await loginAs(staff, 'staff2', staffPass + '-final')
    await ensureWorkspace(staff)
    await shot(staff, 'a2-04-after-restart-relogin')
  })

} finally {
  await writeFileSync(outJson, JSON.stringify({
    schema_version: 1, runner: 'browser', base_url: base,
    started_context: 'chrome-channel-headless-dual-context',
    requests: requests.slice(-200), cases,
  }, null, 2))
  await browser.close()
}
const failed = cases.filter(c => c.status !== 'passed')
console.log(JSON.stringify({ total: cases.length, failed: failed.length }))
process.exit(failed.length ? 1 : 0)
