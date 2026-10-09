<script setup>
import { onMounted, reactive, ref, nextTick } from 'vue'
import api, { errMsg } from '../api'

/* ============================================================ AI 对话 */

const messages = ref([])
const input = ref('')
const sending = ref(false)
const aiEnabled = ref(false)
const listRef = ref(null)

const SUGGESTIONS = [
  '哪台机器磁盘快满了？',
  '当前有哪些未恢复的告警？',
  '列出所有服务器及其在线状态',
  'CPU 使用率最高的机器是哪台？'
]

async function loadStatus() {
  try {
    aiEnabled.value = (await api.get('/ai/status')).data.enabled
  } catch { /* 忽略 */ }
}

async function send(text) {
  const question = (text ?? input.value).trim()
  if (!question || sending.value) return
  messages.value.push({ role: 'user', content: question })
  input.value = ''
  sending.value = true
  await scrollDown()

  try {
    const { data } = await api.post('/ai/ask', { question })
    messages.value.push({
      role: 'assistant',
      content: data.answer,
      toolCalls: data.tool_calls || [],
      model: data.model,
      enabled: data.enabled
    })
  } catch (e) {
    messages.value.push({ role: 'assistant', content: `请求失败：${errMsg(e)}`, error: true })
  } finally {
    sending.value = false
    await scrollDown()
  }
}

async function scrollDown() {
  await nextTick()
  if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
}

/* ============================================================ 模型配置（LLM 设置） */

const llm = ref(null)
const form = reactive({
  enabled: false,
  base_url: '',
  model: '',
  timeout: 60,
  max_tokens: 1024,
  api_key: ''
})
const saving = ref(false)
const testing = ref(false)
const clearing = ref(false)

// 后端 sources 的键名 → 界面文案；值为 db / env / default
const FIELD_LABELS = {
  llm_enabled: '启用 AI',
  llm_base_url: '接口地址',
  llm_model: '模型名',
  llm_timeout: '超时',
  llm_max_tokens: '单次最大 Token',
  llm_api_key: 'API Key'
}
const SOURCE_LABEL = { db: '界面设置', env: '环境变量', default: '默认值' }
const SOURCE_BADGE = { db: 'ok', env: 'warning', default: 'gray' }

function sourceLabel(key) {
  const s = llm.value?.sources?.[key]
  return SOURCE_LABEL[s] || '未知'
}

function sourceBadge(key) {
  const s = llm.value?.sources?.[key]
  return SOURCE_BADGE[s] || 'gray'
}

function applyLlm(data) {
  llm.value = data
  form.enabled = !!data.enabled
  form.base_url = data.base_url || ''
  form.model = data.model || ''
  form.timeout = data.timeout
  form.max_tokens = data.max_tokens
  form.api_key = ''
}

async function loadLlm() {
  try {
    applyLlm((await api.get('/settings/llm')).data)
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function saveLlm() {
  // 只提交改动过的字段，避免覆盖环境变量里配置的其他项
  const orig = llm.value || {}
  const payload = {}
  if (form.enabled !== !!orig.enabled) payload.enabled = form.enabled
  if (form.base_url !== (orig.base_url || '')) payload.base_url = form.base_url
  if (form.model !== (orig.model || '')) payload.model = form.model
  if (Number(form.timeout) !== Number(orig.timeout)) payload.timeout = Number(form.timeout)
  if (Number(form.max_tokens) !== Number(orig.max_tokens)) payload.max_tokens = Number(form.max_tokens)
  // 密钥留空表示不修改；想清空请用「清除密钥」
  if (form.api_key) payload.api_key = form.api_key

  if (!Object.keys(payload).length) {
    window.__toast?.('没有需要保存的改动', 'warn')
    return
  }

  saving.value = true
  try {
    applyLlm((await api.put('/settings/llm', payload)).data)
    await loadStatus()
    window.__toast?.('已保存，立即生效', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    saving.value = false
  }
}

async function testLlm() {
  testing.value = true
  try {
    const { data } = await api.post('/settings/llm/test')
    const extra = data.duration_ms ? ` (${data.duration_ms}ms)` : ''
    const reply = data.reply ? `，模型回复：${data.reply}` : ''
    window.__toast?.(`${data.message}${extra}${data.ok ? reply : ''}`, data.ok ? 'ok' : 'err')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    testing.value = false
  }
}

async function clearApiKey() {
  if (!window.confirm('确定清除已保存的 API Key？清除后将改用环境变量或默认配置。')) return
  clearing.value = true
  try {
    applyLlm((await api.put('/settings/llm', { api_key: '' })).data)
    await loadStatus()
    window.__toast?.('已清除 API Key', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    clearing.value = false
  }
}

/* ============================================================ 访问令牌 */

const tokens = ref([])
const loadingTokens = ref(false)
const showTokenForm = ref(false)
const creatingToken = ref(false)
const createdToken = ref(null)
const tokenForm = reactive({ name: '', scope: 'ai', expires_days: null })

const SCOPE_LABEL = { ai: '可调用 AI', read: '只读' }

function fmtTime(v) {
  if (!v) return '-'
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString('zh-CN', { hour12: false })
}

async function loadTokens() {
  loadingTokens.value = true
  try {
    tokens.value = (await api.get('/tokens')).data
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loadingTokens.value = false
  }
}

function openTokenForm() {
  tokenForm.name = ''
  tokenForm.scope = 'ai'
  tokenForm.expires_days = null
  showTokenForm.value = true
}

async function createToken() {
  if (!tokenForm.name.trim()) {
    window.__toast?.('请填写令牌名称', 'warn')
    return
  }
  const payload = { name: tokenForm.name.trim(), scope: tokenForm.scope }
  const days = Number(tokenForm.expires_days)
  if (tokenForm.expires_days && days > 0) payload.expires_days = days

  creatingToken.value = true
  try {
    const { data } = await api.post('/tokens', payload)
    createdToken.value = data
    showTokenForm.value = false
    await loadTokens()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    creatingToken.value = false
  }
}

async function toggleToken(t) {
  try {
    await api.patch(`/tokens/${t.id}`, { enabled: !t.enabled })
    await loadTokens()
    window.__toast?.(t.enabled ? '已停用' : '已启用', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function removeToken(t) {
  if (!window.confirm(`确定删除令牌「${t.name}」？删除后使用该令牌的外部系统会立即调用失败。`)) return
  try {
    await api.delete(`/tokens/${t.id}`)
    await loadTokens()
    window.__toast?.('已删除', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function copyText(text, label = '内容') {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('当前浏览器不支持自动复制')
    await navigator.clipboard.writeText(text)
    window.__toast?.(`${label}已复制到剪贴板`, 'ok')
  } catch (e) {
    window.__toast?.(`复制失败，请手动选中复制：${e?.message || e}`, 'err')
  }
}

/* ============================================================ 接入说明 */

// 端口动态获取：换了端口示例依然正确
const port = ref(window.location.port || '8848')
const curlExample = () => [
  `curl -X POST http://127.0.0.1:${port.value}/api/v1/ext/ai/chat \\`,
  '  -H "Authorization: Bearer <你的访问令牌>" \\',
  '  -H "Content-Type: application/json" \\',
  '  -d "{\\"question\\":\\"现在有什么问题\\",\\"include_context\\":true}"'
].join('\n')
const openaiExample = () => [
  `base_url = http://127.0.0.1:${port.value}/api/v1/ext/v1`,
  'api_key  = <你的访问令牌>',
  'model    = 任意（平台会用自己的模型配置）'
].join('\n')

onMounted(() => {
  loadStatus()
  loadLlm()
  loadTokens()
})
</script>

<template>
  <div class="ai-page col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">AI 运维助手</h2>
      <span :class="['badge', aiEnabled ? 'ok' : 'gray']">
        {{ aiEnabled ? 'AI 已启用' : 'AI 未配置' }}
      </span>
      <div class="spacer"></div>
    </div>

    <div v-if="!aiEnabled" class="card small">
      <strong>AI 问答当前未启用。</strong>
      平台仍会返回基础统计数据，但无法进行自然语言推理与多步查询。<br />
      现在可以直接在下方「模型配置」里填写密钥并启用，<strong>保存后立即生效，无需重启</strong>；
      也可以用环境变量配置：
      <code class="mono">AIOPS_LLM_ENABLED=true</code>、
      <code class="mono">AIOPS_LLM_API_KEY=你的密钥</code>、
      <code class="mono">AIOPS_LLM_BASE_URL=接口地址</code>、
      <code class="mono">AIOPS_LLM_MODEL=模型名</code>。
      支持任何 OpenAI 兼容端点（DeepSeek / 通义千问 / OpenAI / 本地 Ollama）。
    </div>

    <div ref="listRef" class="chat-list card scroll">
      <div v-if="!messages.length" class="welcome">
        <p class="muted">你可以这样问：</p>
        <div class="suggestions">
          <button v-for="s in SUGGESTIONS" :key="s" class="sm" @click="send(s)">{{ s }}</button>
        </div>
      </div>

      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <div class="msg-head">
          <span class="who">{{ m.role === 'user' ? '我' : 'AI 助手' }}</span>
          <span v-if="m.model" class="small muted">{{ m.model }}</span>
        </div>
        <div :class="['bubble', { err: m.error }]">{{ m.content }}</div>

        <details v-if="m.toolCalls?.length" class="tools">
          <summary class="small muted" style="cursor:pointer">
            调用了 {{ m.toolCalls.length }} 个工具
          </summary>
          <div v-for="(t, j) in m.toolCalls" :key="j" class="tool-call">
            <div class="mono small">→ {{ t.tool }}({{ JSON.stringify(t.arguments) }})</div>
            <pre class="tool-result">{{ JSON.stringify(t.result, null, 2).slice(0, 1200) }}</pre>
          </div>
        </details>
      </div>

      <div v-if="sending" class="msg assistant">
        <div class="msg-head"><span class="who">AI 助手</span></div>
        <div class="bubble muted">正在查询数据…</div>
      </div>
    </div>

    <div class="input-row card">
      <input
        v-model="input"
        placeholder="用自然语言描述你要查的内容，例如：哪台机器磁盘快满了？"
        @keyup.enter="send()"
        :disabled="sending"
      />
      <button class="primary" @click="send()" :disabled="sending || !input.trim()">发送</button>
    </div>

    <!-- ==================================================== 模型配置 -->
    <div class="card">
      <div class="row card-title">
        <strong>模型配置</strong>
        <span v-if="llm" :class="['badge', llm.enabled ? 'ok' : 'gray']">
          {{ llm.enabled ? '已启用' : '未启用' }}
        </span>
        <div class="spacer"></div>
        <span class="small muted" title="每一项的生效来源：界面设置 > 环境变量 > 默认值">
          配置来源：界面设置 &gt; 环境变量 &gt; 默认值
        </span>
      </div>
      <p class="small muted" style="margin:6px 0 12px">
        修改后立即生效，无需重启服务。
      </p>

      <div class="grid2">
        <div>
          <label>启用 AI</label>
          <select v-model="form.enabled">
            <option :value="true">启用</option>
            <option :value="false">停用</option>
          </select>
        </div>
        <div>
          <label>模型名</label>
          <input v-model="form.model" class="mono" placeholder="deepseek-chat" />
        </div>
        <div style="grid-column:1/-1">
          <label>接口地址</label>
          <input v-model="form.base_url" class="mono" placeholder="https://api.deepseek.com/v1" />
        </div>
        <div>
          <label>超时（秒）</label>
          <input v-model.number="form.timeout" type="number" min="5" max="600" />
        </div>
        <div>
          <label>单次最大 Token</label>
          <input v-model.number="form.max_tokens" type="number" min="64" max="32768" />
        </div>
        <div style="grid-column:1/-1">
          <label>API Key</label>
          <div class="row key-row">
            <input
              v-model="form.api_key"
              type="password"
              autocomplete="new-password"
              placeholder="留空表示不修改已保存的密钥"
            />
            <span v-if="llm?.has_api_key" class="badge ok">已配置</span>
            <span v-else class="badge gray">未配置</span>
            <button
              class="sm danger"
              :disabled="!llm?.has_api_key || clearing"
              title="传空字符串清空已保存的密钥"
              @click="clearApiKey"
            >
              {{ clearing ? '清除中…' : '清除密钥' }}
            </button>
          </div>
        </div>
      </div>

      <div class="sources">
        <span class="small muted">各项当前来源：</span>
        <span v-for="(_, key) in FIELD_LABELS" :key="key" class="src-item small">
          {{ FIELD_LABELS[key] }}
          <span :class="['badge', sourceBadge(key)]">{{ sourceLabel(key) }}</span>
        </span>
      </div>

      <div class="row" style="margin-top:14px">
        <button class="primary" :disabled="saving" @click="saveLlm">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <button :disabled="testing" @click="testLlm">
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
      </div>
    </div>

    <!-- ==================================================== 访问令牌 -->
    <div class="card">
      <div class="row card-title">
        <strong>访问令牌</strong>
        <span class="small muted">供外部系统调用平台 AI 能力</span>
        <div class="spacer"></div>
        <button class="sm" :disabled="loadingTokens" @click="loadTokens">刷新</button>
        <button class="sm primary" @click="openTokenForm">新建令牌</button>
      </div>

      <div v-if="!tokens.length" class="empty">
        还没有访问令牌。点击「新建令牌」，把平台 AI 能力开放给外部系统。
      </div>
      <table v-else style="margin-top:10px">
        <thead>
          <tr>
            <th>名称</th><th>令牌前缀</th><th>权限范围</th><th>状态</th>
            <th>调用次数</th><th>最后使用时间</th><th style="width:150px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in tokens" :key="t.id">
            <td>
              <strong>{{ t.name }}</strong>
              <div class="small muted">{{ t.created_by ? `创建者 ${t.created_by}` : '' }}</div>
            </td>
            <td class="mono small">{{ t.prefix }}…</td>
            <td>
              <span :class="['badge', t.scope === 'ai' ? 'info' : 'gray']">
                {{ SCOPE_LABEL[t.scope] || t.scope }}
              </span>
            </td>
            <td>
              <span :class="['badge', t.enabled ? 'ok' : 'offline']">
                {{ t.enabled ? '启用' : '停用' }}
              </span>
              <span v-if="t.expired" class="badge critical" style="margin-left:4px">已过期</span>
              <div v-if="t.expires_at" class="small muted">到期 {{ fmtTime(t.expires_at) }}</div>
              <div v-else class="small muted">长期有效</div>
            </td>
            <td class="small">{{ t.call_count || 0 }}</td>
            <td class="small muted">{{ fmtTime(t.last_used_at) }}</td>
            <td>
              <button class="sm" @click="toggleToken(t)">{{ t.enabled ? '停用' : '启用' }}</button>
              <button class="sm danger" style="margin-left:4px" @click="removeToken(t)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ==================================================== 接入说明 -->
    <div class="card">
      <div class="row card-title">
        <strong>如何接入</strong>
        <div class="spacer"></div>
        <span class="small muted">
          平台地址按当前端口 {{ port }} 生成，可直接复制使用
        </span>
      </div>

      <div class="howto">
        <div class="row">
          <strong style="font-size:13px">方式一（平台原生接口）</strong>
          <div class="spacer"></div>
          <button class="sm" @click="copyText(curlExample(), '示例命令')">复制</button>
        </div>
        <pre class="mono">{{ curlExample() }}</pre>
      </div>

      <div class="howto">
        <div class="row">
          <strong style="font-size:13px">方式二（OpenAI 兼容，任何支持自定义 base_url 的客户端都能接）</strong>
          <div class="spacer"></div>
          <button class="sm" @click="copyText(openaiExample(), '示例配置')">复制</button>
        </div>
        <pre class="mono">{{ openaiExample() }}</pre>
      </div>
    </div>

    <!-- 新建令牌表单 -->
    <div v-if="showTokenForm" class="modal-mask" @click.self="showTokenForm = false">
      <div class="modal">
        <h3>新建访问令牌</h3>
        <div class="grid2">
          <div style="grid-column:1/-1">
            <label>名称 *</label>
            <input v-model="tokenForm.name" placeholder="例如：外部工单系统" @keyup.enter="createToken" />
          </div>
          <div>
            <label>权限范围</label>
            <select v-model="tokenForm.scope">
              <option value="ai">可调用 AI</option>
              <option value="read">只读</option>
            </select>
          </div>
          <div>
            <label>
              有效期天数
              <span class="muted">（留空表示长期有效）</span>
            </label>
            <input v-model.number="tokenForm.expires_days" type="number" min="1" max="3650" placeholder="长期有效" />
          </div>
        </div>
        <p class="small muted" style="margin:12px 0 0">
          明文令牌只在创建成功后显示一次，请务必立即保存。
        </p>
        <div class="row" style="margin-top:20px;justify-content:flex-end">
          <button :disabled="creatingToken" @click="showTokenForm = false">取消</button>
          <button class="primary" :disabled="creatingToken" @click="createToken">
            {{ creatingToken ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 明文令牌只显示这一次 -->
    <div v-if="createdToken" class="modal-mask" @click.self="createdToken = null">
      <div class="modal" style="width:640px">
        <h3>令牌创建成功</h3>
        <div class="token-alert">
          <strong>⚠ 请立即保存，关闭后无法再次查看。</strong>
          数据库里只保存哈希值，平台之后无法再还原这串明文令牌。
        </div>
        <label>你的访问令牌（{{ createdToken.name }}）</label>
        <div class="token-box mono">{{ createdToken.token }}</div>
        <div class="row" style="margin-top:10px">
          <button class="primary" @click="copyText(createdToken.token, '令牌')">复制令牌</button>
          <span class="small muted">
            权限范围：{{ SCOPE_LABEL[createdToken.scope] || createdToken.scope }}；
            前缀：<span class="mono">{{ createdToken.prefix }}…</span>
          </span>
        </div>
        <div class="row" style="margin-top:20px;justify-content:flex-end">
          <button @click="createdToken = null">我已保存，关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ai-page { gap: 12px; }
.chat-list { flex: none; height: 380px; padding: 16px; display: flex; flex-direction: column; gap: 14px; min-height: 0; }
.welcome { margin: auto; text-align: center; }
.suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 10px; }
.msg { display: flex; flex-direction: column; gap: 5px; max-width: 88%; }
.msg.user { align-self: flex-end; align-items: flex-end; }
.msg-head { display: flex; gap: 8px; align-items: center; }
.who { font-size: 12px; color: var(--text-dim); }
.bubble {
  padding: 10px 14px; border-radius: 9px; background: var(--bg-soft);
  white-space: pre-wrap; line-height: 1.65; font-size: 13px;
}
.msg.user .bubble { background: #1e3a5f; }
.bubble.err { background: #3b1d1d; color: #fca5a5; }
.tools { margin-top: 4px; }
.tool-call { margin-top: 6px; }
.tool-result {
  background: #0b0f14; padding: 8px 10px; border-radius: 5px; font-size: 11px;
  max-height: 200px; overflow: auto; margin: 4px 0 0; white-space: pre-wrap;
}
.input-row { display: flex; gap: 10px; padding: 10px 12px; }
code { background: var(--bg-soft); padding: 1px 5px; border-radius: 4px; font-size: 12px; }

/* 模型配置 */
.card-title { margin-bottom: 2px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.sources {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px;
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);
}
.src-item { display: inline-flex; align-items: center; gap: 5px; }
.key-row > *:not(input) { flex-shrink: 0; }

/* 接入说明 */
.howto { margin-top: 14px; }
.howto pre {
  background: #0b0f14; padding: 12px 14px; border-radius: 7px; margin: 8px 0 0;
  font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-all;
  overflow-x: auto; color: var(--text);
}

/* 明文令牌提示 */
.token-alert {
  background: #422f0d; color: #fbbf24; border-left: 3px solid var(--warn);
  padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 13px;
  line-height: 1.7; margin-bottom: 14px;
}
.token-alert strong { color: #fde68a; }
.token-box {
  background: #0b0f14; border: 1px dashed var(--accent); padding: 12px 14px;
  border-radius: 7px; font-size: 13px; word-break: break-all; line-height: 1.6;
  color: #93c5fd; user-select: all;
}
</style>
