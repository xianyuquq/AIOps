<script setup>
import { onMounted, ref, nextTick } from 'vue'
import api, { errMsg } from '../api'

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

onMounted(loadStatus)
</script>

<template>
  <div class="chat-page">
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
      启用方式：设置环境变量后重启服务 ——
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
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; gap: 12px; height: 100%; }
.chat-list { flex: 1; padding: 16px; display: flex; flex-direction: column; gap: 14px; min-height: 0; }
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
</style>
