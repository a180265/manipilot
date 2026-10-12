// R7-S2 第三批：真实 TTL 绝对过期（独立栈，TTL=6s；非禁用/重置/退出替代）。
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createRequire } from 'node:module'
const exec = promisify(execFile)

const [, , base, outJson, shotsDir, adminPass, frontendRoot] = process.argv
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
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
page.on('response', res => requests.push({ method: res.request().method(),
                                           url: res.url(), status: res.status() }))
const shot = name => page.screenshot({ path: resolve(shotsDir, `${name}.png`) })
const tid = id => page.getByTestId(id)

try {
  await step('ttl-absolute-expiry', '真实 8h 语义经 TTL=6s 配置推进：同 Cookie 会话越过绝对到期 401 回登录', async () => {
    await page.goto(base + '/', { waitUntil: 'networkidle' })
    await tid('login-username').fill('admin')
    await tid('login-password').fill(adminPass)
    // 首登改密（如需）
    await page.getByRole('button', { name: /^登录$/ }).click()
    await page.waitForTimeout(800)
    if (await tid('temp-password').count()) {
      await tid('temp-password').fill(adminPass)
      await tid('new-password').fill(adminPass + '-ok')
      await tid('confirm-password').fill(adminPass + '-ok')
      await page.getByRole('button', { name: /修改并重新登录/ }).click()
      await tid('login-username').waitFor({ timeout: 9000 })
      await tid('login-username').fill('admin')
      await tid('login-password').fill(adminPass + '-ok')
      await page.getByRole('button', { name: /^登录$/ }).click()
    }
    await tid('account-trigger').waitFor({ timeout: 9000 })
    const live = await page.evaluate(async () =>
      (await fetch('/sales-api/internal/wx-auth/v1/session',
                   { credentials: 'include' })).status)
    if (live !== 200) throw new Error(`TTL 内会话期望 200 实际 ${live}`)
    await shot('a3-01-live-session')
    // 越过绝对 TTL（服务端 WX_ACCOUNT_SESSION_TTL_SECONDS=6）
    await new Promise(r => setTimeout(r, 7500))
    const expired = await page.evaluate(async () =>
      (await fetch('/sales-api/internal/wx-auth/v1/session',
                   { credentials: 'include' })).status)
    if (expired !== 401) throw new Error(`越过绝对 TTL 期望 401 实际 ${expired}`)
    // 页面：重新加载后会话预检失败 → 登录页（非退出/禁用/重置路径）
    await page.goto(base + '/', { waitUntil: 'networkidle' })
    await tid('login-username').waitFor({ timeout: 9000 })
    await shot('a3-02-expired-back-to-login')
  })
} finally {
  await writeFileSync(outJson, JSON.stringify({
    schema_version: 1, runner: 'browser', base_url: base,
    started_context: 'chrome-channel-headless', requests: requests.slice(-100), cases,
  }, null, 2))
  await browser.close()
}
const failed = cases.filter(c => c.status !== 'passed')
console.log(JSON.stringify({ total: cases.length, failed: failed.length }))
process.exit(failed.length ? 1 : 0)
