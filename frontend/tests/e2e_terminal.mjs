/**
 * 核心需求端到端验证（真实浏览器 + 真实 SSH）：
 *
 *   在 4 台服务器绑定一个广播会话，在其中一个终端输入 `df -Th`，
 *   验证 (1) 四个面板实时显示相同输入 (2) 回车后四台同时执行
 *        (3) 四台各自返回输出。
 *
 * 前置条件：
 *   1. python backend/tests/mock_ssh_servers.py   （4 个 mock 节点）
 *   2. uvicorn app.main:app --port 8848           （平台）
 *   3. python backend/tests/seed_mock_servers.py  （注册节点）
 *
 * 运行：node tests/e2e_terminal.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:8848'
const OUT = 'E:/Agent/docs/screenshots'
mkdirSync(OUT, { recursive: true })

const failures = []
function check(name, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`)
  if (!ok) failures.push(name)
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })

const consoleErrors = []
page.on('pageerror', (e) => consoleErrors.push(e.message))
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text())
})

// ---------------------------------------------------------- 登录
await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
await page.fill('input[autocomplete="username"]', 'admin')
await page.fill('input[type="password"]', 'admin123')
await page.click('button[type="submit"]')
await page.waitForURL('**/#/dashboard', { timeout: 15000 })
check('登录成功', true)

// ---------------------------------------------------------- 进入多终端
await page.goto(`${BASE}/#/terminal`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)

// 清空默认选择，只选 4 台 mock 节点
const clearBtn = page.locator('button:has-text("清空")')
if (await clearBtn.count()) await clearBtn.click()

const mockNames = ['web-mock-01', 'web-mock-02', 'web-mock-03', 'web-mock-04']
let picked = 0
for (const name of mockNames) {
  const pick = page.locator('.pick', { hasText: name })
  if ((await pick.count()) > 0) {
    await pick.first().click()
    picked++
  }
}
check('选中 4 台 mock 服务器', picked === 4, `实际 ${picked} 台`)
if (picked === 0) {
  console.log('SKIP  未找到 mock 服务器，请先运行 mock_ssh_servers.py 与 seed_mock_servers.py')
  await browser.close()
  process.exit(1)
}

// 布局设为 4 台
await page.selectOption('select:below(:text("台"))', '4').catch(() => {})
await page.waitForTimeout(300)

// ---------------------------------------------------------- 建立会话
await page.click('button.primary:has-text("建立会话")')
await page.waitForSelector('.xterm', { timeout: 25000 })
await page.waitForTimeout(3500)

const paneCount = await page.locator('.pane').count()
check('创建了 4 个终端面板', paneCount === 4, `实际 ${paneCount}`)

const readyBadges = await page.locator('.pane .badge:has-text("ready")').count()
check('4 台终端均已就绪', readyBadges === 4, `ready=${readyBadges}`)
await page.screenshot({ path: `${OUT}/10-terminal-connected.png`, fullPage: true })

// 读取各面板可见文本的辅助函数
async function paneTexts() {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('.pane')).map((p) => {
      const rows = p.querySelector('.xterm-rows')
      return rows ? rows.innerText : ''
    })
  )
}

// ---------------------------------------------------------- 输入 df -Th（逐字符）
const firstPaneTerm = page.locator('.pane .xterm').first()
await firstPaneTerm.click()
await page.waitForTimeout(400)

const COMMAND = 'df -Th'
await page.keyboard.type(COMMAND, { delay: 60 })
await page.waitForTimeout(900)

// 验证：输入过程中四个面板显示一致（本地回显生效）
const duringTyping = await paneTexts()
const allSame = duringTyping.every((t) => t.includes(COMMAND))
check('★ 输入实时同步到全部 4 个面板', allSame,
  allSame ? '' : JSON.stringify(duringTyping.map((t) => t.slice(-40))))
await page.screenshot({ path: `${OUT}/11-typed-synced.png`, fullPage: true })

// ---------------------------------------------------------- 回车：四台同时执行
await page.keyboard.press('Enter')
await page.waitForTimeout(3500)

const afterRun = await paneTexts()

// 每台都应返回 df 结果
const withResult = afterRun.filter((t) => t.includes('Filesystem') && t.includes('/dev/sda1'))
check('★ 回车后 4 台同时执行并各自返回结果', withResult.length === 4,
  `含结果的面板数 = ${withResult.length}`)

// 内容一致性：命令输出必须完全相同。
// 注意不能直接比较整屏文本——每台终端会显示自己的主机名提示符
// （[web-mock-01]$ / [web-mock-02]$ ...），这是正确的按机分流行为。
const dfLines = (t) =>
  t
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.includes('Filesystem') || s.startsWith('/dev/'))

const perPaneDf = afterRun.map(dfLines)
const sameOutput = perPaneDf.every(
  (lines) => lines.length > 0 && JSON.stringify(lines) === JSON.stringify(perPaneDf[0])
)
check('4 台命令输出完全一致（结果同步）', sameOutput,
  sameOutput ? `${perPaneDf[0].length} 行输出` : JSON.stringify(perPaneDf))

// 反向验证：每台显示的是自己的主机名，说明输出按 server_id 正确分流
const ownName = afterRun.every((t, i) => t.includes(`web-mock-0${i + 1}`))
check('各终端显示自身主机名（输出正确分流）', ownName)

await page.screenshot({ path: `${OUT}/12-broadcast-result.png`, fullPage: true })

// ---------------------------------------------------------- 命令输入栏同时执行
const cmdInput = page.locator('.cmdbar input')
if (await cmdInput.count()) {
  await cmdInput.fill('uname -a')
  await cmdInput.press('Enter')
  await page.waitForTimeout(2500)
  const afterBar = await paneTexts()
  const unameCount = afterBar.filter((t) => t.includes('GNU/Linux')).length
  check('命令输入栏同样广播到全部终端', unameCount === 4, `${unameCount}/4`)
  await page.screenshot({ path: `${OUT}/13-commandbar.png`, fullPage: true })
}

// ---------------------------------------------------------- 危险命令拦截
if (await cmdInput.count()) {
  await cmdInput.fill('rm -rf /')
  await cmdInput.press('Enter')
  await page.waitForTimeout(1500)
  const bodyText = await page.locator('body').innerText()
  check('危险命令被拦截并提示', bodyText.includes('高危') || bodyText.includes('拦截'))
}

// ---------------------------------------------------------- 审计日志已记录
await page.goto(`${BASE}/#/audit`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
const auditText = await page.locator('body').innerText()
check('命令已写入审计日志', auditText.includes('df -Th'), '')
await page.screenshot({ path: `${OUT}/14-audit.png`, fullPage: true })

const meaningful = consoleErrors.filter((e) => !e.includes('favicon'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 3).join(' | '))

await browser.close()

console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
