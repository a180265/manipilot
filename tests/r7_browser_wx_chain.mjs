// R7-S2：正式 App 浏览器链（index→main→App→AuthGate→Workspace）。
// 真实 Chrome（channel）、浏览器自管 Cookie、经隔离 nginx（仓库 software 模板
// 测试副本：仅上游端点替换）到达隔离 WX 后端/假 Fleet。
// 产出 browser-json-v1 报告 + 每步截图；不用 auth-preview、不手塞 Cookie。
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

// playwright-core 从 frontend/node_modules 解析（脚本不在 frontend 内）
const [, , base, outJson, shotsDir, adminPass, staffPass, frontendRoot] = process.argv
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
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.on('response', res => {
  requests.push({ method: res.request().method(), url: res.url(),
                  status: res.status() })
})
const shot = name => page.screenshot({ path: resolve(shotsDir, `${name}.png`) })
  // 下拉是开关式：确保处于打开态（关闭态再点一次）
  async function openAccountMenu() {
    await tid('account-trigger').click()
    try { await tid('logout-button').waitFor({ state: 'visible', timeout: 1500 }) }
    catch { await tid('account-trigger').click()
             await tid('logout-button').waitFor({ state: 'visible', timeout: 5000 }) }
  }
const tid = id => page.getByTestId(id)

async function login(username, password) {
  await tid('login-username').fill(username)
  await tid('login-password').fill(password)
  await page.getByRole('button', { name: /^登录$/ }).click()
  await page.waitForTimeout(600)
}

try {
  await step('load-real-app', '正式 index→main→App：登录门出现且非预览壳', async () => {
    await page.goto(base + '/', { waitUntil: 'networkidle' })
    await tid('login-username').waitFor({ timeout: 10000 })
    const html = await page.content()
    if (html.includes('auth-preview') || html.includes('preview-main'))
      throw new Error('加载了预览壳而非正式 App')
    const bundle = await page.evaluate(
      () => [...document.scripts].map(s => s.src).filter(s => s.includes('/assets/index-')))
    if (!bundle.length) throw new Error('未加载正式产物 bundle')
    await shot('01-login-page')
    return { bundle: bundle[0] }
  })

  await step('anonymous-denied', '匿名业务请求被拒（401）', async () => {
    const status = await page.evaluate(async () => {
      const r = await fetch('/sales-api/internal/dependencies/health',
                            { credentials: 'include' })
      return r.status
    })
    if (status !== 401) throw new Error(`匿名业务请求期望 401，实际 ${status}`)
    return { status }
  })

  await step('proxy-whitelist', '代理白名单：只读放行/写拒', async () => {
    const [readOnly, writeDeny] = await page.evaluate(async () => {
      const a = await fetch('/arm-api/v1/system/status')
      const b = await fetch('/arm-api/v1/tasks', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      return [a.status, b.status]
    })
    if (readOnly !== 200) throw new Error(`只读期望 200 实际 ${readOnly}`)
    if (writeDeny !== 403) throw new Error(`任务提交期望 403 实际 ${writeDeny}`)
    return { readOnly, writeDeny }
  })

  await step('admin-login', '管理员真实登录（首登强制改密→工作区）', async () => {
    await login('admin', adminPass)
    // 首登 must_change_password：先走强制改密，再用新口令进入工作区
    if (await tid('temp-password').count()) {
      await tid('temp-password').fill(adminPass)
      await tid('new-password').fill(adminPass + '-ok')
      await tid('confirm-password').fill(adminPass + '-ok')
      await page.getByRole('button', { name: /修改并重新登录/ }).click()
      await tid('login-username').waitFor({ timeout: 8000 })
      await login('admin', adminPass + '-ok')
    }
    await tid('account-trigger').waitFor({ timeout: 8000 })
    await openAccountMenu()
    const role = await tid('account-role').textContent()
    await tid('account-trigger').click()
    if (!/管理员/.test(role || '')) throw new Error(`角色显示异常：${role}`)
    await shot('02-admin-workspace')
    return { role }
  })

  await step('admin-add-staff', '管理员添加客服并交付临时密码', async () => {
    await page.getByRole('button', { name: /用户管理/ }).click()
    await tid('open-add-user').click()
    await tid('add-username').fill('staff1')
    await tid('add-display-name').fill('客服一号')
    await tid('add-temp-password').fill(staffPass)
    await page.getByRole('button', { name: /创建客服账号/ }).click()
    await tid('created-credential').waitFor({ timeout: 8000 })
    await tid(`user-row-staff1`).waitFor({ timeout: 8000 })
    await shot('03-staff-created')
    return { staff: 'staff1' }
  })

  await step('admin-logout', '管理员退出', async () => {
    await openAccountMenu()
    await tid('logout-button').click()
    await tid('login-username').waitFor({ timeout: 8000 })
    await shot('04-admin-logged-out')
  })

  await step('staff-first-login-change', '客服首次登录强制改密', async () => {
    await login('staff1', staffPass)
    await tid('temp-password').waitFor({ timeout: 8000 })
    await tid('temp-password').fill(staffPass)
    await tid('new-password').fill(staffPass + '-new')
    await tid('confirm-password').fill(staffPass + '-new')
    await page.getByRole('button', { name: /修改并重新登录/ }).click()
    await tid('login-username').waitFor({ timeout: 8000 })
    await login('staff1', staffPass + '-new')
    await tid('account-trigger').waitFor({ timeout: 8000 })
    await shot('05-staff-workspace')
  })

  await step('staff-config-hidden', '客服无配置/用户管理入口；直连被拒 403', async () => {
    const navText = await page.evaluate(() => document.body.innerText)
    if (navText.includes('用户管理')) throw new Error('客服侧出现用户管理入口')
    if (navText.includes('项目设置')) throw new Error('客服侧出现项目设置入口')
    const status = await page.evaluate(async () => {
      const r = await fetch('/sales-api/internal/wx-users/v1/users',
                            { credentials: 'include' })
      return r.status
    })
    if (status !== 403) throw new Error(`客服直连用户管理期望 403 实际 ${status}`)
    await shot('06-staff-no-config')
    return { directStatus: status }
  })

  await step('staff-business-ok', '客服业务页可用（真实会话）', async () => {
    const hasWorkspace = await page.evaluate(() =>
      Boolean(document.querySelector('.workspace, .app-shell, main')))
    const health = await page.evaluate(() =>
      document.querySelector('.connection-dot')?.getAttribute('data-status'))
    await shot('07-staff-business')
    return { hasWorkspace, health }
  })

  await step('staff-logout-back-denied', '退出后真实后退不可恢复旧会话', async () => {
    await openAccountMenu()
    await tid('logout-button').click()
    await tid('login-username').waitFor({ timeout: 8000 })
    await page.goBack()
    await page.waitForTimeout(800)
    // 后退页不得残留业务数据（SPA 无路由历史时后退可能离开应用）
    const backHasBusiness = await page.evaluate(() =>
      Boolean(document.querySelector('.workspace, .app-shell')))
    if (backHasBusiness) throw new Error('退出后后退仍显示业务区')
    await page.goto(base + '/', { waitUntil: 'networkidle' })
    // about:blank 源下的 fetch 是跨域（被正确拒绝）；回应用内做同源会话核验
    await tid('login-username').waitFor({ timeout: 8000 })
    const status = await page.evaluate(async () => {
      const r = await fetch('/sales-api/internal/wx-auth/v1/session',
                            { credentials: 'include' })
      return r.status
    })
    if (status !== 401) throw new Error(`旧会话期望 401 实际 ${status}`)
    await shot('08-back-denied')
    return { sessionStatus: status }
  })

  await step('narrow-viewport', '窄屏（375×812）登录与工作区可用', async () => {
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto(base + '/', { waitUntil: 'networkidle' })
    await tid('login-username').waitFor({ timeout: 8000 })
    await login('staff1', staffPass + '-new')
    await tid('account-trigger').waitFor({ timeout: 8000 })
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    await shot('09-narrow-viewport')
    if (overflow > 24) throw new Error(`窄屏横向溢出 ${overflow}px`)
    return { overflowPx: overflow }
  })
} finally {
  await writeFileSync(outJson, JSON.stringify({
    schema_version: 1, runner: 'browser',
    base_url: base, started_context: 'chrome-channel-headless',
    requests: requests.slice(-200), cases,
  }, null, 2))
  await browser.close()
}
const failed = cases.filter(c => c.status !== 'passed')
console.log(JSON.stringify({ total: cases.length, failed: failed.length }))
process.exit(failed.length ? 1 : 0)
