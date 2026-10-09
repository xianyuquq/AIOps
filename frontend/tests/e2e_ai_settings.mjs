/**
 * 验证「AI 助手」页新增的模型配置、访问令牌、接入说明三块界面。
 *
 * 运行：node tests/e2e_ai_settings.mjs
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
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } })
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

await page.goto(`${BASE}/#/ai`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

let body = await page.locator('body').innerText()

// ---------- 模型配置 ----------
check('★ 存在「模型配置」区块', body.includes('模型配置'))

// 用标题精确匹配卡片：问答卡片的正文里也提到了「模型配置」这四个字，
// 用 hasText 会误匹配到它（实测踩过）。
const cfg = page.locator('.card:has(.card-title strong:text-is("模型配置"))').first()
check('模型配置卡片可见', await cfg.isVisible().catch(() => false))

const cfgText = await cfg.innerText().catch(() => '')
for (const label of ['接口地址', '模型名', 'API Key']) {
  check(`配置项含「${label}」`, cfgText.includes(label))
}

// 输入框应已回填当前配置
const inputs = await cfg.locator('input').count()
check('有可编辑的输入框', inputs >= 3, `${inputs} 个 input`)

// 不假设输入框顺序，只断言「卡片里有一个值是 http(s) 地址的输入框」
const inputValues = await cfg.locator('input').evaluateAll((els) => els.map((e) => e.value))
check('接口地址已回填', inputValues.some((v) => /^https?:\/\//.test(v)),
  inputValues.map((v) => String(v).slice(0, 30)).join(' | '))

// 来源徽章
const srcLabels = ['界面设置', '环境变量', '默认值']
check('显示了配置来源', srcLabels.some((s) => cfgText.includes(s)),
  srcLabels.filter((s) => cfgText.includes(s)).join('/') || '无')

// 测试连接按钮
const testBtn = cfg.locator('button', { hasText: '测试连接' }).first()
check('★ 有「测试连接」按钮', await testBtn.isVisible().catch(() => false))

// 真正点一次：AI 未配置时应给出明确失败提示，而不是静默
if (await testBtn.isVisible().catch(() => false)) {
  await testBtn.click()
  await page.waitForTimeout(6000)
  const after = await page.locator('body').innerText()
  const toast = page.locator('[class*=toast], .toast').first()
  const toastText = (await toast.innerText().catch(() => '')) || ''
  const signalled =
    /连接成功|未启用|失败|错误|Error|无法/.test(toastText) ||
    /连接成功|未启用|失败/.test(after)
  check('★ 点击测试连接后有明确反馈', signalled, (toastText || '').slice(0, 60))
}

// ---------- 访问令牌 ----------
body = await page.locator('body').innerText()
check('★ 存在「访问令牌」区块', body.includes('访问令牌'))

const tokCard = page.locator('.card:has(.card-title strong:text-is("访问令牌"))').first()
const tokText = await tokCard.innerText().catch(() => '')
check('有「新建令牌」入口', /新建|创建/.test(tokText))

const createBtn = tokCard.locator('button', { hasText: /新建|创建/ }).first()
check('新建按钮可见', await createBtn.isVisible().catch(() => false))

await page.screenshot({ path: `${OUT}/70-ai-settings.png`, fullPage: true })

// ---------- 接入说明 ----------
body = await page.locator('body').innerText()
check('★ 存在接入说明', /接入|如何接入|调用/.test(body))
check('给出了 /ext/ai/chat 示例', body.includes('/ext/ai/chat'))
check('给出了 OpenAI 兼容端点', body.includes('/ext/v1'))
check('示例端口与当前访问端口一致',
  body.includes(`:${new URL(BASE).port}`) || body.includes('8848'))

// ---------- 接口层面核对 ----------
const api = await page.evaluate(async () => {
  const token = localStorage.getItem('aiops_token')
  const h = { Authorization: `Bearer ${token}` }
  const [s, t] = await Promise.all([
    fetch('/api/v1/settings/llm', { headers: h }).then((r) => r.json()),
    fetch('/api/v1/tokens', { headers: h }).then((r) => r.json())
  ])
  return { settings: s, tokens: Array.isArray(t) ? t.length : -1 }
})
check('设置接口返回 sources', api.settings && 'sources' in api.settings)
check('★ 设置接口不返回密钥明文',
  !JSON.stringify(api.settings).includes('"api_key"') ||
    api.settings.api_key === undefined)
check('令牌接口可用', api.tokens >= 0, `${api.tokens} 条`)

const meaningful = errors.filter((e) => !e.includes('favicon'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 2).join(' | '))

await browser.close()
console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
