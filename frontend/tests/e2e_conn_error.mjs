/**
 * 验证「连接失败不再静默」：
 *  用一台故意连不上的服务器建立会话，确认界面明确显示失败原因，
 *  而不是只显示 offline 却没有任何说明。
 *
 * 运行：node tests/e2e_conn_error.mjs
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
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })
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
check('登录成功', true)

// ---------- 确保存在一台「连不上」的服务器 ----------
// 用一个保留端口（必然无人监听）建一台测试服务器，模拟连接失败场景
const setup = await page.evaluate(async () => {
  const token = localStorage.getItem('aiops_token')
  const h = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }

  // 已存在则复用
  const list = await (await fetch('/api/v1/servers', { headers: h })).json()
  let srv = list.find((s) => s.name === '连不通测试机')
  if (!srv) {
    const resp = await fetch('/api/v1/servers', {
      method: 'POST',
      headers: h,
      body: JSON.stringify({
        name: '连不通测试机',
        host: '127.0.0.1',
        port: 59999,
        username: 'root',
        protocol: 'ssh',
        auth_type: 'password',
        secret: 'x',
        tags: {}
      })
    })
    srv = await resp.json()
  }
  return srv
})
check('已准备一台连不通的服务器', !!setup?.id, `id=${setup?.id}`)

// ---------- 走 API 诊断，确认返回明确的失败原因 ----------
const diag = await page.evaluate(async (id) => {
  const token = localStorage.getItem('aiops_token')
  const r = await fetch(`/api/v1/servers/${id}/diagnose`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` }
  })
  return r.json()
}, setup.id)
check('诊断接口返回分阶段结果', Array.isArray(diag.steps) && diag.steps.length > 0,
  `${diag.steps?.length} 个阶段`)
const failedStep = diag.steps?.find((s) => !s.ok)
check('诊断明确指出失败原因（非空）', !!failedStep?.detail?.trim(),
  failedStep?.detail?.slice(0, 60))
check('诊断结论可读（非纯异常名）',
  !!failedStep?.detail && failedStep.detail.length > 10, '')

// ---------- 走 /test，确认错误消息非空 ----------
const testResp = await page.evaluate(async (id) => {
  const token = localStorage.getItem('aiops_token')
  const r = await fetch(`/api/v1/servers/${id}/test`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` }
  })
  return r.json()
}, setup.id)
check('/test 返回的失败原因非空', !testResp.ok && !!testResp.message?.trim(),
  testResp.message?.slice(0, 70))

// ---------- 打开终端会话，确认面板显示错误而不是一片空白 ----------
await page.goto(`${BASE}/#/terminal`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1000)

const clearBtn = page.locator('button:has-text("清空")')
if (await clearBtn.count()) await clearBtn.click()

const pick = page.locator('.pick', { hasText: '连不通测试机' })
if (await pick.count()) {
  await pick.first().click()

  // toast 只显示 3.6 秒，必须边等边收集，否则检查时早已消失
  const seenToasts = []
  const collect = setInterval(async () => {
    try {
      const texts = await page.locator('.toast').allInnerTexts()
      for (const t of texts) if (!seenToasts.includes(t)) seenToasts.push(t)
    } catch { /* 页面可能正在切换 */ }
  }, 250)

  await page.click('button.primary:has-text("建立会话")')
  await page.waitForTimeout(7000)
  clearInterval(collect)

  const bodyText = await page.locator('body').innerText()
  const hasPaneError = (await page.locator('.pane-error').count()) > 0
  check('终端面板显示连接失败提示块', hasPaneError)

  const errText = hasPaneError
    ? await page.locator('.pane-error .err-msg').first().innerText()
    : ''
  check('面板中显示了具体失败原因', errText.trim().length > 10, errText.slice(0, 70))
  check('失败原因不是空泛的「连接失败：」',
    !/连接失败[:：]\s*$/.test(errText.trim()), errText.slice(0, 40))

  const toastText = seenToasts.join(' | ')
  check('同时弹出错误提示', /连接失败|诊断/.test(toastText), toastText.slice(0, 80))

  await page.screenshot({ path: `${OUT}/20-conn-error.png`, fullPage: true })

  // 诊断按钮可用
  const diagBtn = page.locator('.pane-error button:has-text("诊断")')
  if (await diagBtn.count()) {
    await diagBtn.first().click()
    await page.waitForTimeout(3500)
    const modalVisible = await page.locator('.diag-text').isVisible().catch(() => false)
    check('点击诊断弹出分阶段诊断结果', modalVisible)
    await page.screenshot({ path: `${OUT}/21-diagnosis.png`, fullPage: true })
  }
} else {
  check('找到测试服务器条目', false, '未找到')
}

const meaningful = errors.filter((e) => !e.includes('favicon') && !e.includes('59999'))
check('无浏览器运行时错误', meaningful.length === 0, meaningful.slice(0, 2).join(' | '))

await browser.close()
console.log()
if (failures.length) {
  console.log(`${failures.length} 项失败：${failures.join(', ')}`)
  process.exit(1)
}
console.log('全部通过')
