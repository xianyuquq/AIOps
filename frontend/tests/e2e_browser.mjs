/**
 * 真实浏览器端到端验证。
 *
 * 验证内容：
 *  1. SPA 能在浏览器中真正挂载运行（捕获运行时 JS 错误）
 *  2. 登录流程可用
 *  3. 每个页面都能正常渲染
 *  4. 多终端页面的 xterm.js 能实例化
 *
 * 运行：node tests/e2e_browser.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:8848'
const OUT = 'E:/Agent/docs/screenshots'
mkdirSync(OUT, { recursive: true })

const failures = []
const consoleErrors = []

function check(name, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`)
  if (!ok) failures.push(name)
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })

page.on('console', (msg) => {
  if (msg.type() === 'error') consoleErrors.push(msg.text())
})
page.on('pageerror', (err) => {
  consoleErrors.push(`PAGEERROR: ${err.message}`)
})

// ---------------------------------------------------------------- 登录页
await page.goto(BASE, { waitUntil: 'networkidle' })
check('页面标题正确', (await page.title()).includes('运维'), await page.title())
check('未登录时跳转到登录页', page.url().includes('/login'), page.url())

const loginVisible = await page.locator('input[type="password"]').isVisible()
check('登录表单已渲染', loginVisible)
await page.screenshot({ path: `${OUT}/01-login.png` })

// ---------------------------------------------------------------- 登录
await page.fill('input[autocomplete="username"]', 'admin')
await page.fill('input[type="password"]', 'admin123')
await page.click('button[type="submit"]')
await page.waitForURL('**/#/dashboard', { timeout: 15000 })
check('登录成功并进入大盘', page.url().includes('/dashboard'))
await page.waitForTimeout(2500)
await page.screenshot({ path: `${OUT}/02-dashboard.png`, fullPage: true })

const brand = await page.locator('.sidebar').isVisible()
check('侧边栏导航已渲染', brand)

const statCards = await page.locator('.stat').count()
check('大盘统计卡片已渲染', statCards >= 4, `${statCards} 个`)

// ---------------------------------------------------------------- 各页面渲染
const pages = [
  ['/servers', '服务器管理'],
  ['/scripts', '监控脚本'],
  ['/alerts', '告警中心'],
  ['/metrics', '监控图表'],
  ['/ai', 'AI 运维助手'],
  ['/databases', '数据库监控'],
  ['/files', '文件传输'],
  ['/audit', '审计日志']
]

for (const [path, expectText] of pages) {
  await page.goto(BASE + '/#' + path, { waitUntil: 'networkidle' })
  await page.waitForTimeout(700)
  const body = await page.locator('body').innerText()
  check(`页面 ${path} 渲染出「${expectText}」`, body.includes(expectText))
  await page.screenshot({ path: `${OUT}/page${path.replace('/', '-')}.png` })
}

// ---------------------------------------------------------------- 多终端（核心）
await page.goto(BASE + '/#/terminal', { waitUntil: 'networkidle' })
await page.waitForTimeout(900)
const termBody = await page.locator('body').innerText()
check('多终端页面渲染', termBody.includes('多终端'))
check('显示服务器选择区', termBody.includes('选择服务器') || termBody.includes('还没有服务器'))
await page.screenshot({ path: `${OUT}/03-terminal.png`, fullPage: true })

// 若已有服务器，验证 xterm 能实例化
const serverCount = await page.locator('.pick').count()
if (serverCount > 0) {
  await page.locator('.pick').first().click()
  await page.click('button.primary:has-text("建立会话")')
  await page.waitForTimeout(4000)
  const xtermCount = await page.locator('.xterm').count()
  check('xterm 终端实例已创建', xtermCount > 0, `${xtermCount} 个`)
  await page.screenshot({ path: `${OUT}/04-terminal-session.png`, fullPage: true })
} else {
  console.log('SKIP  终端会话测试（尚无服务器，需真实 SSH 目标）')
}

// ---------------------------------------------------------------- AI 助手（降级路径）
await page.goto(BASE + '/#/ai', { waitUntil: 'networkidle' })
await page.waitForTimeout(700)
const aiBody = await page.locator('body').innerText()
check('AI 未配置时给出启用指引', aiBody.includes('未启用') || aiBody.includes('已启用'))
await page.screenshot({ path: `${OUT}/05-ai.png`, fullPage: true })

// ---------------------------------------------------------------- 控制台错误
const meaningful = consoleErrors.filter(
  (e) => !e.includes('favicon') && !e.includes('404 (Not Found)')
)
check('无未预期的浏览器运行时错误', meaningful.length === 0,
  meaningful.length ? meaningful.slice(0, 4).join(' | ') : '')

await browser.close()

console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过；截图已保存到 docs/screenshots/')
