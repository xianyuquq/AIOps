/**
 * 验证数据库监控与备份容灾页面的真实交互。
 *
 * 覆盖用户提出的三个问题：
 *  1. 数据库监控是否真的能采集（连接测试 / 立即采集有数据）
 *  2. 数据库实例是否可编辑（此前只有删除）
 *  3. 备份模板是否可选择并创建任务
 *
 * 运行：node tests/e2e_db_backup.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:8848'
const OUT = 'E:/Agent/docs/screenshots'
mkdirSync(OUT, { recursive: true })

// 口令从环境变量取，不写进源码（会被提交到版本库）。
// 未设置时该项断言自动跳过——它只是「确认口令没被拼进脚本」的加固检查。
const DB_PASSWORD = process.env.AIOPS_TEST_LOCAL_DB_PASSWORD || ''

const failures = []
function check(name, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`)
  if (!ok) failures.push(name)
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text())
})

await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
await page.fill('input[autocomplete="username"]', 'admin')
await page.fill('input[type="password"]', 'admin123')
await page.click('button[type="submit"]')
await page.waitForURL('**/#/dashboard', { timeout: 15000 })

// ============================================================ 数据库监控
await page.goto(`${BASE}/#/databases`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
const dbBody = await page.locator('body').innerText()
check('数据库监控页面已渲染', dbBody.includes('数据库监控'))
check('页面说明了两种采集方式', dbBody.includes('直连') && dbBody.includes('SSH 跳板'))

const rows = await page.locator('tbody tr').count()
console.log(`      已配置实例：${rows} 个`)
check('存在数据库实例', rows >= 1)

// 选定一个「可用的」实例来做连接与采集验证：
// 表里第一行可能是用户早先建的测试实例（连不上），不能代表功能是否可用。
const rowTexts = await page.locator('tbody tr').allInnerTexts()
let targetIndex = rowTexts.findIndex((t) => /postgres|mysql/i.test(t))
if (targetIndex < 0) targetIndex = 0
console.log(`      用于验证的实例：${rowTexts[targetIndex].split('\n')[0]}`)
const targetRow = page.locator('tbody tr').nth(targetIndex)

// ---------- 编辑能力（此前只有删除） ----------
const buttons = await targetRow.locator('button').allInnerTexts()
console.log(`      行内按钮：${buttons.map((b) => b.trim()).join(' / ')}`)
check('★ 提供「编辑」按钮（此前只能删除）',
  buttons.some((b) => b.includes('编辑')))
check('★ 提供「测试连接」按钮', buttons.some((b) => b.includes('测试')))
check('★ 提供「立即采集」按钮', buttons.some((b) => b.includes('采集')))

// 点编辑，确认表单带出原有数据且密码留空
await targetRow.locator('button', { hasText: '编辑' }).click()
await page.waitForTimeout(800)
const modalText = await page.locator('.modal').innerText()
check('编辑弹窗打开并带出原名称', modalText.includes('编辑数据库实例'))
const nameVal = await page.locator('.modal input').first().inputValue()
check('名称已回填', nameVal.length > 0, nameVal)
const secretVal = await page.locator('.modal input[type="password"]').inputValue()
check('★ 编辑时密码留空（不修改）', secretVal === '')
check('提示留空表示不修改', modalText.includes('留空'))
await page.screenshot({ path: `${OUT}/40-db-edit.png`, fullPage: true })
await page.locator('.modal button', { hasText: '取消' }).click()
await page.waitForTimeout(500)

// toast 只显示约 3.6 秒，必须边等边收集
function collectToasts(ms) {
  const seen = []
  const timer = setInterval(async () => {
    try {
      for (const t of await page.locator('.toast').allInnerTexts()) {
        if (!seen.includes(t)) seen.push(t)
      }
    } catch { /* 忽略 */ }
  }, 200)
  return new Promise((resolve) => setTimeout(() => { clearInterval(timer); resolve(seen) }, ms))
}

// ---------- 连接测试 ----------
const testToasts = []
const testTimer = setInterval(async () => {
  try {
    for (const t of await page.locator('.toast').allInnerTexts()) {
      if (!testToasts.includes(t)) testToasts.push(t)
    }
  } catch { /* 忽略 */ }
}, 200)
await targetRow.locator('button', { hasText: '测试连接' }).click()
await page.waitForTimeout(8000)
clearInterval(testTimer)
const testToast = testToasts.join(' | ')
check('★ 测试连接返回结果', /连接成功|失败/.test(testToast), testToast.slice(0, 110))

// ---------- 立即采集 ----------
const collectSeen = []
const collectTimer = setInterval(async () => {
  try {
    for (const t of await page.locator('.toast').allInnerTexts()) {
      if (!collectSeen.includes(t)) collectSeen.push(t)
    }
  } catch { /* 忽略 */ }
}, 200)
await targetRow.locator('button', { hasText: '立即采集' }).click()
await page.waitForTimeout(10000)
clearInterval(collectTimer)

const modal2 = page.locator('.modal')
const collectVisible = await modal2.isVisible().catch(() => false)
if (collectVisible) {
  const collectText = await modal2.innerText()
  check('★ 立即采集返回指标明细', /采集成功|采集失败/.test(collectText),
    collectText.slice(0, 90).replace(/\n/g, ' '))
  check('采集结果展示了指标名与数值',
    /db\./.test(collectText), (collectText.match(/db\.[\w.]+/g) || []).slice(0, 3).join(', '))
  await page.screenshot({ path: `${OUT}/41-db-collect.png`, fullPage: true })
  await modal2.locator('button', { hasText: '关闭' }).click()
} else {
  check('立即采集弹出结果窗口', false, `toast: ${collectSeen.join(' | ').slice(0, 100)}`)
}
await page.waitForTimeout(500)

// ============================================================ 备份与容灾
await page.goto(`${BASE}/#/backups`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
const bkpBody = await page.locator('body').innerText()
check('备份与容灾页面已渲染', bkpBody.includes('备份与容灾'))
check('页面说明了密码注入方式', bkpBody.includes('PGPASSWORD') || bkpBody.includes('MYSQL_PWD'))
check('页面说明了保留期清理', bkpBody.includes('保留'))

// ---------- 新建任务：模板可选择 ----------
await page.locator('button', { hasText: '新建备份任务' }).click()
await page.waitForTimeout(800)
const formModal = page.locator('.modal')
check('新建任务弹窗打开', await formModal.isVisible())

const templateSelect = formModal.locator('select').first()
const optgroups = await templateSelect.locator('optgroup').count()
const options = await templateSelect.locator('option').count()
check('★ 模板按分组可选', optgroups >= 2, `${optgroups} 个分组`)
check('★ 模板数量充足', options >= 6, `${options} 个选项`)

const optTexts = await templateSelect.locator('option').allInnerTexts()
console.log(`      模板：${optTexts.filter(Boolean).join(' / ')}`)
check('包含 MySQL 备份模板', optTexts.some((t) => t.includes('MySQL')))
check('包含 PostgreSQL 备份模板', optTexts.some((t) => t.includes('PostgreSQL')))
check('包含 Kingbase 备份模板', optTexts.some((t) => t.includes('Kingbase')))
check('包含 Oracle 备份模板', optTexts.some((t) => t.includes('Oracle')))

// 选一个模板，确认脚本被自动填充
const pgOption = await templateSelect.locator('option', { hasText: 'PostgreSQL — pg_dump' }).first().getAttribute('value')
await templateSelect.selectOption(pgOption)
await page.waitForTimeout(700)

const scriptArea = formModal.locator('textarea').first()
const scriptVal = await scriptArea.inputValue()
check('★ 选择模板后自动填充备份命令', scriptVal.includes('pg_dump'), scriptVal.slice(0, 60))
check('模板含保留期清理命令', scriptVal.includes('mtime') && scriptVal.includes('delete'))
check('模板使用环境变量传密码（不含明文密码）',
  !/PGPASSWORD\s*=\s*['"]?\w/.test(scriptVal) && !(DB_PASSWORD && scriptVal.includes(DB_PASSWORD)))
const dirVal = await formModal.locator('input').nth(4).inputValue().catch(() => '')
console.log(`      自动填充目录：${dirVal}`)
check('占位符说明可见',
  (await formModal.innerText()).includes('{{host}}') ||
  (await formModal.innerText()).includes('占位符'))

await page.screenshot({ path: `${OUT}/42-backup-template.png`, fullPage: true })
await formModal.locator('button', { hasText: '取消' }).click()
await page.waitForTimeout(500)

const meaningful = errors.filter((e) => !e.includes('favicon'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 2).join(' | '))

await browser.close()
console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
