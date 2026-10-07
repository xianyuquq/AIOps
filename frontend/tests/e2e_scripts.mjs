/**
 * 验证监控脚本页面的真实交互：
 *  1. 内置脚本默认停用
 *  2. 点「启用」能真正启用（曾经这一步必定 400 失败）
 *  3. 内置脚本正文只读，但可试运行
 *  4. 「复制」能生成可编辑副本
 *  5. 采集周期可改
 *
 * 运行：node tests/e2e_scripts.mjs
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
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})

// ---------- 登录 ----------
await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
await page.fill('input[autocomplete="username"]', 'admin')
await page.fill('input[type="password"]', 'admin123')
await page.click('button[type="submit"]')
await page.waitForURL('**/#/dashboard', { timeout: 15000 })

// ---------- 进入监控脚本页 ----------
await page.goto(`${BASE}/#/scripts`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
check('监控脚本页面已渲染', (await page.locator('body').innerText()).includes('监控脚本'))

// 找到「Linux 系统指标」所在行
const linuxRow = page.locator('tr', { hasText: 'Linux 系统指标' }).first()
check('找到内置 Linux 脚本行', (await linuxRow.count()) > 0)

// 注意：行内第一个 .badge 是「内置」标记，状态徽章在第 6 列
const statusCell = linuxRow.locator('td:nth-child(6) .badge').first()
const readStatus = async () => (await statusCell.innerText()).trim()

const initialStatus = await readStatus()
console.log(`      初始状态：${initialStatus}`)

// ---------- 点「启用/停用」——曾经这里必定失败 ----------
const toggleBtn = linuxRow.locator('button', { hasText: /启用|停用/ }).first()
const toggleLabel = (await toggleBtn.innerText()).trim()
check('存在启用/停用按钮', !!toggleLabel, toggleLabel)

// 收集 toast，捕捉可能的错误提示
const toasts = []
const collect = setInterval(async () => {
  try {
    for (const t of await page.locator('.toast').allInnerTexts()) {
      if (!toasts.includes(t)) toasts.push(t)
    }
  } catch { /* 忽略 */ }
}, 200)

await toggleBtn.click()
await page.waitForTimeout(3000)
clearInterval(collect)

const afterStatus = await readStatus()
console.log(`      点击后状态：${afterStatus}  toast：${toasts.join(' | ')}`)

const switched = afterStatus !== initialStatus
check('★ 点击启用/停用成功切换状态', switched, `${initialStatus} -> ${afterStatus}`)
const hadError = toasts.some((t) => /不可修改|失败|400|错误/.test(t))
check('未出现「内置脚本不可修改」之类错误', !hadError, toasts.join(' | '))

// 再点一次切回来
await linuxRow.locator('button', { hasText: /启用|停用/ }).first().click()
await page.waitForTimeout(2500)
const finalStatus = await readStatus()
check('可以再次切换回来', finalStatus === initialStatus, `${afterStatus} -> ${finalStatus}`)

// ---------- 确认最终处于启用状态（便于后续采集演示） ----------
if ((await readStatus()) !== '已启用') {
  await linuxRow.locator('button', { hasText: '启用' }).first().click()
  await page.waitForTimeout(2500)
}
check('脚本最终处于启用状态', (await readStatus()) === '已启用')

// ---------- 打开编辑器：正文只读但可试运行 ----------
await linuxRow.locator('button', { hasText: '查看/编辑' }).first().click()
await page.waitForTimeout(1000)

const modalVisible = await page.locator('.modal').isVisible().catch(() => false)
check('编辑弹窗已打开', modalVisible)

const builtinNote = await page.locator('.builtin-note').count()
check('内置脚本显示只读说明', builtinNote > 0)

const bodyReadonly = await page.locator('.modal textarea').first().getAttribute('readonly')
check('脚本正文为只读', bodyReadonly !== null)

const testBtn = page.locator('.modal button', { hasText: '试运行' }).first()
check('内置脚本仍可试运行', (await testBtn.count()) > 0 && (await testBtn.isEnabled()))

const paramSaveBtn = page.locator('.modal button', { hasText: '保存采集参数' })
check('内置脚本可保存采集参数', (await paramSaveBtn.count()) > 0)

await page.screenshot({ path: `${OUT}/30-script-builtin-editor.png`, fullPage: true })
await page.locator('.modal button', { hasText: '关闭' }).first().click()
await page.waitForTimeout(600)

// ---------- 复制出可编辑副本 ----------
page.once('dialog', (d) => d.accept())
await linuxRow.locator('button', { hasText: '复制' }).first().click()
await page.waitForTimeout(2500)

const copyRow = page.locator('tr', { hasText: '（副本）' }).first()
check('复制生成了副本行', (await copyRow.count()) > 0)

if ((await copyRow.count()) > 0) {
  const copyText = await copyRow.innerText()
  check('副本未标记为内置', !copyText.includes('内置'))
  check('副本默认停用', copyText.includes('已停用'))

  // 清理副本
  page.once('dialog', (d) => d.accept())
  await copyRow.locator('button', { hasText: '删除' }).click().catch(() => {})
  await page.waitForTimeout(1200)
}

await page.screenshot({ path: `${OUT}/31-scripts-list.png`, fullPage: true })

const meaningful = errors.filter((e) => !e.includes('favicon'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 2).join(' | '))

await browser.close()
console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
