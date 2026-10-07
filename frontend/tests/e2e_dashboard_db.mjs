/**
 * 验证「数据库指标在总览大盘可见」。
 *
 * 背景：用户反馈数据库指标采到了，但总览页完全看不到数据库情况。
 * 本用例锁定大盘必须展示数据库概况与关键指标。
 *
 * 运行：node tests/e2e_dashboard_db.mjs
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
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
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
await page.waitForTimeout(2500)

const body = await page.locator('body').innerText()

// ---------- 统计卡 ----------
check('大盘有「数据库在线」统计卡', body.includes('数据库在线'))
check('统计卡显示了数据库数量（分母）', /\d+\s*\/\s*\d+/.test(body))

// ---------- 数据库概况区块 ----------
check('★ 大盘存在「数据库概况」区块', body.includes('数据库概况'))

const dbCard = page.locator('.card', { hasText: '数据库概况' }).first()
const cardVisible = await dbCard.isVisible().catch(() => false)
check('数据库概况卡片可见', cardVisible)

const rows = await dbCard.locator('tbody tr').count()
console.log(`      概况表行数：${rows}`)
check('★ 概况表列出了数据库实例', rows >= 1, `${rows} 个实例`)

if (rows > 0) {
  const headers = await dbCard.locator('thead th').allInnerTexts()
  console.log(`      表头：${headers.join(' | ')}`)
  for (const col of ['实例', '类型', '状态', '连接数', '命中率']) {
    check(`包含「${col}」列`, headers.some((h) => h.includes(col)))
  }

  const firstRow = await dbCard.locator('tbody tr').first().innerText()
  console.log(`      首行：${firstRow.replace(/\n/g, ' | ')}`)
  check('★ 显示了连接数具体值', /\d+/.test(firstRow))
  check('★ 显示了缓冲命中率', /%/.test(firstRow))
  check('状态徽章已渲染', /在线|异常|未知/.test(firstRow))
}

// ---------- 接口层面 ----------
const api = await page.evaluate(async () => {
  const token = localStorage.getItem('aiops_token')
  const r = await fetch('/api/v1/dashboard/summary', {
    headers: { Authorization: `Bearer ${token}` }
  })
  return r.json()
})
check('大盘接口返回 database_total', api.database_total !== undefined, String(api.database_total))
check('大盘接口返回 database_online', api.database_online !== undefined, String(api.database_online))
check('★ 大盘接口返回 databases 明细', Array.isArray(api.databases) && api.databases.length > 0,
  `${api.databases?.length} 条`)

if (api.databases?.length) {
  const d = api.databases[0]
  for (const f of ['id', 'name', 'db_type', 'status', 'connections', 'hit_rate', 'metrics']) {
    check(`明细含字段 ${f}`, f in d)
  }
  check('归一化出连接数使用率', 'connection_usage_pct' in d)
}

await page.screenshot({ path: `${OUT}/50-dashboard-db.png`, fullPage: true })

// ---------- 数据库列表页也应有实时值 ----------
await page.goto(`${BASE}/#/databases`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)
const dbBody = await page.locator('body').innerText()
check('数据库列表页显示实时连接数', /连接\s*\d+/.test(dbBody), '')
check('数据库列表页显示命中率', /命中\s*[\d.]+%/.test(dbBody), '')
await page.screenshot({ path: `${OUT}/51-databases-live.png`, fullPage: true })

const meaningful = errors.filter((e) => !e.includes('favicon'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 2).join(' | '))

await browser.close()
console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
