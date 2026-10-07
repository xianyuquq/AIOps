/**
 * 验证大盘「趋势预测」区块。
 *
 * 运行：node tests/e2e_forecast.mjs
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
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } })
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
await page.waitForTimeout(3000)

const body = await page.locator('body').innerText()
check('★ 大盘存在「趋势预测」区块', body.includes('趋势预测'))

const card = page.locator('.card', { hasText: '趋势预测' }).first()
check('预测卡片可见', await card.isVisible().catch(() => false))

const rows = await card.locator('tbody tr').count()
console.log(`      预测行数：${rows}`)
check('★ 预测表有数据', rows >= 1, `${rows} 条`)

if (rows > 0) {
  const headers = await card.locator('thead th').allInnerTexts()
  console.log(`      表头：${headers.join(' | ')}`)
  for (const col of ['对象', '指标', '当前值', '日均变化', '预测', '可信度']) {
    check(`包含「${col}」列`, headers.some((h) => h.includes(col)))
  }

  const texts = await card.locator('tbody tr').allInnerTexts()
  const joined = texts.join('\n')
  console.log('      预测内容：')
  for (const t of texts.slice(0, 6)) {
    console.log('        ' + t.replace(/\n/g, ' | '))
  }

  check('★ 显示了增长率（/天）', /\/天/.test(joined))
  check('★ 显示了拟合优度 R²', /R²=/.test(joined))
  check('显示了可信度', /(高|中|低)/.test(joined))

  // 关键：波动型指标不应出现「马上耗尽」这类误导性外推
  const hasPeakNote = /窗口内峰值|余量/.test(joined)
  check('★ 波动型指标用峰值余量而非外推', hasPeakNote, hasPeakNote ? '' : '未找到峰值说明')

  // 页面应说明两类预测的区别
  check('页面解释了增长型/波动型的区别', body.includes('波动型') || body.includes('峰值'))
}

// ---------- 接口层面 ----------
const api = await page.evaluate(async () => {
  const token = localStorage.getItem('aiops_token')
  const r = await fetch('/api/v1/dashboard/summary', {
    headers: { Authorization: `Bearer ${token}` }
  })
  return r.json()
})
check('接口返回 forecasts', Array.isArray(api.forecasts))
check('★ forecasts 非空', api.forecasts.length > 0, `${api.forecasts.length} 条`)

for (const f of api.forecasts) {
  check(`预测项含 mode 字段 (${f.metric_key})`, 'mode' in f)
}
const peakItems = api.forecasts.filter((f) => f.mode === 'peak')
const growthItems = api.forecasts.filter((f) => f.mode === 'growth')
console.log(`      增长型 ${growthItems.length} 条，波动型 ${peakItems.length} 条`)

// 波动型不得给出线性外推的剩余时间
const badPeak = peakItems.filter((f) => f.hours_to_limit !== null && f.hours_to_limit !== undefined)
check('★ 波动型指标没有线性外推的剩余时间', badPeak.length === 0,
  badPeak.map((f) => `${f.target_name}/${f.label}=${f.hours_to_limit}h`).join(', '))

// 低可信度不得给出剩余时间
const badConf = api.forecasts.filter(
  (f) => f.confidence === 'low' && f.mode === 'growth' && f.eta && f.eta.includes('后')
)
check('★ 低可信度不给出剩余时间', badConf.length === 0,
  badConf.map((f) => `${f.target_name}/${f.label}`).join(', '))

await page.screenshot({ path: `${OUT}/60-dashboard-forecast.png`, fullPage: true })

const meaningful = errors.filter((e) => !e.includes('favicon'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 2).join(' | '))

await browser.close()
console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
