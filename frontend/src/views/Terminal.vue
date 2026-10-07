<script setup>
/**
 * 多终端广播界面 —— 需求 2 的核心实现。
 *
 * 同步原理（详见 docs/ARCHITECTURE.md §6.3）：
 *  1. 用户在任一面板输入时，先**本地回显到所有面板**（零延迟、视觉完全一致），
 *     再把字符通过 WebSocket 广播给后端，由后端并行写入各 SSH 通道。
 *  2. 远端已通过 stty -echo 关闭回显，因此服务器返回的都是纯命令输出，
 *     不会与本地回显重复。
 *  3. 回车作为普通输入一起广播，保证四台在同一事件循环 tick 内执行。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import api, { errMsg, getToken } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

const servers = ref([])
const groups = ref([])
const selectedGroup = ref('')
const selected = ref([])
const layout = ref(4)
const mode = ref('mirror')
const connected = ref(false)
const sessionId = ref('')
const commandInput = ref('')
const statuses = ref({})
const showPicker = ref(true)
const diagnosis = ref(null)

const terms = shallowRef([])       // [{ serverId, name, term, fit }]
const gridRef = ref(null)
let ws = null
let fitTimer = null

const selectedServers = computed(() =>
  servers.value.filter((s) => selected.value.includes(s.id))
)

const visibleSlots = computed(() => {
  const list = selectedServers.value.slice()
  while (list.length < layout.value && list.length < selected.value.length) break
  return list
})

const gridStyle = computed(() => {
  const n = Math.max(visibleSlots.value.length, 1)
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 6 ? 3 : 4
  const rows = Math.ceil(n / cols)
  return {
    gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
    gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`
  }
})

async function load() {
  const [s, g] = await Promise.all([api.get('/servers'), api.get('/servers/groups')])
  servers.value = s.data
  groups.value = g.data
  if (!selected.value.length && servers.value.length) {
    selected.value = servers.value.slice(0, Math.min(4, servers.value.length)).map((x) => x.id)
    layout.value = Math.max(1, Math.min(4, selected.value.length))
  }
}

function toggleServer(id) {
  const i = selected.value.indexOf(id)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(id)
}

function applyGroup() {
  if (!selectedGroup.value) return
  selected.value = servers.value
    .filter((s) => s.group_id === selectedGroup.value)
    .map((s) => s.id)
}

/* ------------------------------------------------------------------ WebSocket */

function connect() {
  if (ws && ws.readyState <= 1) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws/terminal?token=${encodeURIComponent(getToken())}`)

  ws.onopen = () => {
    connected.value = true
  }

  ws.onclose = () => {
    connected.value = false
    sessionId.value = ''
  }

  ws.onerror = () => {
    connected.value = false
  }

  ws.onmessage = (event) => {
    let msg
    try {
      msg = JSON.parse(event.data)
    } catch {
      return
    }
    handleMessage(msg)
  }
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'session.ready': {
      sessionId.value = msg.session_id
      // 合并而非覆盖：session.ready 在连接尝试之后才到达，
      // 若直接覆盖会把先前 server.status 带回来的失败原因清空
      // （这正是「连不上却不显示原因」的根因之一）。
      const merged = {}
      for (const s of msg.servers) {
        const prev = statuses.value[s.id]
        merged[s.id] = {
          status: s.status,
          message: s.error || prev?.message || ''
        }
      }
      statuses.value = merged

      // 连接失败的服务器逐个提示，避免用户只看到 offline
      for (const s of msg.servers) {
        if (s.status === 'offline') {
          window.__toast?.(
            `${s.name} 连接失败：${s.error || '未知原因'}`,
            'err'
          )
        }
      }

      nextTick(() => {
        buildTerminals()
        doFit()
      })
      break
    }

    case 'output': {
      const t = terms.value.find((x) => x.serverId === msg.server_id)
      if (t) t.term.write(msg.data)
      break
    }

    case 'server.status': {
      if (msg.server_id) {
        const prev = statuses.value[msg.server_id] || {}
        const next = {
          status: msg.status,
          message: msg.message || prev.message || ''
        }
        statuses.value = { ...statuses.value, [msg.server_id]: next }

        // 连接失败必须显式提示，否则用户只看到 offline 却不知道原因
        if (msg.status === 'offline') {
          const name = visibleSlots.value.find((s) => s.id === msg.server_id)?.name
          window.__toast?.(`${name || '服务器'} 连接失败：${msg.message || '未知原因'}`, 'err')
        }
      }
      break
    }

    case 'broadcast_ack':
      if (msg.server_ids?.length) {
        window.__toast?.(`已同步执行到 ${msg.server_ids.length} 台服务器`, 'ok')
      }
      break

    case 'command.blocked':
      window.__toast?.(msg.message, 'err')
      break

    case 'mode.changed':
      mode.value = msg.mode
      break

    case 'session.closed':
      sessionId.value = ''
      break

    case 'error':
      window.__toast?.(msg.message, 'err')
      break

    case 'pong':
      break
  }
}

function send(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj))
}

/* ------------------------------------------------------------------ 终端实例 */

function buildTerminals() {
  disposeTerminals()
  const list = visibleSlots.value
  const created = []

  for (const server of list) {
    const el = document.getElementById(`term-${server.id}`)
    if (!el) continue

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "'Cascadia Code', Consolas, 'Noto Sans Mono CJK SC', monospace",
      theme: {
        background: '#0b0f14',
        foreground: '#d8e0ea',
        cursor: '#3b82f6',
        selectionBackground: '#264f78'
      },
      scrollback: 4000,
      // convertEol: 把裸 \n 也当作文末回车换行处理。
      // 真实 PTY 会通过 ONLCR 把 \n 翻译成 \r\n，但经过某些跳板/非标准
      // shell 时可能只收到裸 \n；若不转换，光标只下移不回列首，
      // 输出会从半截位置开始写而错位。对该设置而言 \r\n 输入行为不变，
      // 因此是严格更健壮的选择。
      convertEol: true
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    term.open(el)
    try {
      fit.fit()
    } catch { /* 面板尚未布局完成 */ }

    term.writeln(`\x1b[36m● ${server.name} (${server.host})\x1b[0m`)
    term.writeln('\x1b[90m等待连接…\x1b[0m')

    const entry = { serverId: server.id, name: server.name, term, fit }

    term.onData((data) => {
      onTerminalInput(server.id, data, entry)
    })

    created.push(entry)
  }

  terms.value = created
}

function onTerminalInput(serverId, data, entry) {
  if (!sessionId.value) return

  if (mode.value === 'mirror' || mode.value === 'template') {
    // ★ 本地立即回显到**所有**面板（包括正在输入的这个）。
    // xterm 本身不会回显按键，且远端已 stty -echo，
    // 因此当前面板若不显式写入就会一片空白。
    //
    // 回车（\r）需显示为 \r\n，让光标进入下一行与远端输出衔接；
    // 远端因关闭回显不会重复输出这一行。
    const display = data.replace(/\r/g, '\r\n')
    for (const t of terms.value) {
      t.term.write(display)
    }
    // 再广播给后端并行写入各 SSH 通道 —— 四台在同一事件循环 tick 内收到
    send({ type: 'broadcast_input', session_id: sessionId.value, data })
  } else {
    // diff / single：只作用于当前终端，由远端回显
    send({ type: 'input', session_id: sessionId.value, server_id: serverId, data })
  }
}

function disposeTerminals() {
  for (const t of terms.value) {
    try {
      t.term.dispose()
    } catch { /* 忽略 */ }
  }
  terms.value = []
}

function doFit() {
  for (const t of terms.value) {
    try {
      t.fit.fit()
    } catch { /* 忽略 */ }
  }
  if (terms.value.length) {
    const first = terms.value[0].term
    send({
      type: 'resize',
      session_id: sessionId.value,
      cols: first.cols,
      rows: first.rows
    })
  }
}

/* ------------------------------------------------------------------ 会话控制 */

function openSession() {
  if (!selected.value.length) {
    window.__toast?.('请先选择服务器', 'warn')
    return
  }
  connect()
  const start = () => {
    send({
      type: 'session.create',
      server_ids: selected.value.slice(0, layout.value),
      mode: mode.value,
      cols: 120,
      rows: 30
    })
    showPicker.value = false
  }
  if (ws?.readyState === 1) start()
  else ws?.addEventListener('open', start, { once: true })
}

function closeSession() {
  if (sessionId.value) send({ type: 'session.close', session_id: sessionId.value })
  sessionId.value = ''
  disposeTerminals()
  showPicker.value = true
}

function changeMode(newMode) {
  mode.value = newMode
  if (sessionId.value) send({ type: 'mode.change', session_id: sessionId.value, mode: newMode })
}

/** 命令输入栏：回车即广播到所有终端同时执行。 */
function runCommand() {
  const cmd = commandInput.value.trim()
  if (!cmd || !sessionId.value) return
  commandInput.value = ''

  if (mode.value === 'template') {
    // 模板模式：每台服务器用各自的名称/主机替换占位符，逐台下发。
    // 占位符需在服务端逐台写入，无法用一次广播完成。
    for (const s of visibleSlots.value) {
      const rendered = cmd
        .replaceAll('{{host}}', s.name)
        .replaceAll('{{ip}}', s.host || '')
      send({
        type: 'input',
        session_id: sessionId.value,
        server_id: s.id,
        data: rendered + '\r'
      })
      // 远端回显已关闭，本地自行显示这一行
      const t = terms.value.find((x) => x.serverId === s.id)
      if (t) t.term.write(rendered + '\r\n')
    }
    window.__toast?.(`已按模板下发到 ${visibleSlots.value.length} 台服务器`, 'ok')
    return
  }

  // 镜像 / 差异 / 单机：统一广播，四台在同一 tick 内执行。
  // 本地先回显，让所有面板都看到这一行（远端不回显）。
  for (const t of terms.value) {
    t.term.write(cmd + '\r\n')
  }
  send({ type: 'run', session_id: sessionId.value, command: cmd })
}

function sendCtrlC() {
  if (sessionId.value) send({ type: 'broadcast_input', session_id: sessionId.value, data: '\x03' })
}

/* ------------------------------------------------------------------ 状态与诊断 */

function statusClass(status) {
  if (status === 'ready') return 'ok'
  if (status === 'offline' || status === 'closed') return 'offline'
  return 'unknown'
}

function statusLabel(status) {
  if (status === 'ready') return 'ready'
  if (status === 'offline') return '连接失败'
  if (status === 'closed') return '已关闭'
  return status || '连接中'
}

/** 运行分阶段连接诊断，把每一步的结论直接显示出来。 */
async function diagnose(server) {
  window.__toast?.(`正在诊断 ${server.name} …`)
  try {
    const { data } = await api.post(`/servers/${server.id}/diagnose`)
    const lines = data.steps.map(
      (s) => `${s.ok ? '✓' : '✕'} ${s.step}\n    ${s.detail}`
    )
    diagnosis.value = {
      name: server.name,
      target: data.target,
      ok: data.ok,
      text: lines.join('\n')
    }
    if (!data.ok) {
      const failed = data.steps.find((s) => !s.ok)
      window.__toast?.(`诊断完成：${failed?.detail?.slice(0, 80) || '存在问题'}`, 'err')
    } else {
      window.__toast?.('诊断通过：连接正常', 'ok')
    }
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
  fitTimer = setInterval(() => {
    if (terms.value.length && !showPicker.value) doFit()
  }, 4000)
  window.addEventListener('resize', doFit)
})

onBeforeUnmount(() => {
  clearInterval(fitTimer)
  window.removeEventListener('resize', doFit)
  if (sessionId.value) send({ type: 'session.close', session_id: sessionId.value })
  disposeTerminals()
  try {
    ws?.close()
  } catch { /* 忽略 */ }
})

const modeHelp = {
  mirror: '镜像：输入实时同步到所有终端（推荐用于 df -Th 这类相同操作）',
  diff: '差异：只在自己终端输入，各机命令可不同',
  single: '单机：只操作当前终端，适合单独排查',
  template: '模板：命令中的 {{host}} 会按机器名替换'
}
</script>

<template>
  <div class="page">
    <div class="toolbar card">
      <div class="row" style="flex-wrap:wrap;gap:10px">
        <strong>多终端</strong>
        <span :class="['badge', connected ? 'ok' : 'unknown']">
          {{ connected ? 'WebSocket 已连接' : '未连接' }}
        </span>

        <select v-model="mode" style="width:190px" @change="changeMode(mode)" :disabled="!sessionId">
          <option value="mirror">镜像同步（默认）</option>
          <option value="diff">差异模式</option>
          <option value="single">单机模式</option>
        </select>

        <select v-model.number="layout" style="width:110px" :disabled="!!sessionId">
          <option :value="1">1 台</option>
          <option :value="2">2 台</option>
          <option :value="4">4 台</option>
          <option :value="6">6 台</option>
          <option :value="9">9 台</option>
        </select>

        <div class="spacer"></div>

        <button v-if="!sessionId" class="primary" @click="openSession">建立会话</button>
        <template v-else>
          <button @click="sendCtrlC" title="向所有终端发送 Ctrl+C">Ctrl+C</button>
          <button class="danger" @click="closeSession">关闭会话</button>
        </template>
      </div>

      <p class="small muted" style="margin:8px 0 0">{{ modeHelp[mode] }}</p>
    </div>

    <!-- 命令输入栏：回车同时执行 -->
    <div v-if="sessionId" class="card cmdbar">
      <span class="mono prompt">$</span>
      <input
        v-model="commandInput"
        class="mono"
        placeholder="输入命令后回车，将在所有终端同时执行（例如 df -Th）"
        @keyup.enter="runCommand"
      />
      <button class="primary" @click="runCommand">同时执行</button>
    </div>

    <!-- 终端网格 -->
    <div v-if="sessionId" ref="gridRef" class="grid" :style="gridStyle">
      <div v-for="s in visibleSlots" :key="s.id" class="pane">
        <div class="pane-head">
          <span class="pane-title">{{ s.name }}</span>
          <span class="muted small mono">{{ s.host }}</span>
          <div class="spacer"></div>
          <button
            v-if="statuses[s.id]?.status === 'offline'"
            class="sm"
            @click="diagnose(s)"
          >
            诊断
          </button>
          <span :class="['badge', statusClass(statuses[s.id]?.status)]">
            {{ statusLabel(statuses[s.id]?.status) }}
          </span>
        </div>
        <div :id="`term-${s.id}`" class="term-host"></div>
        <!-- 连接失败时覆盖显示原因，避免用户面对一片空白 -->
        <div v-if="statuses[s.id]?.status === 'offline'" class="pane-error">
          <div class="err-title">✕ 无法连接到 {{ s.name }}</div>
          <div class="err-msg">{{ statuses[s.id]?.message || '服务端未返回原因' }}</div>
          <button class="sm" @click="diagnose(s)">运行连接诊断</button>
        </div>
      </div>
    </div>

    <!-- 服务器选择 -->
    <div v-else class="card">
      <div class="row" style="margin-bottom:14px">
        <strong>选择服务器</strong>
        <select v-model="selectedGroup" style="width:180px" @change="applyGroup">
          <option value="">按分组筛选…</option>
          <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
        </select>
        <div class="spacer"></div>
        <span class="muted small">已选 {{ selected.length }} 台，本次会话使用前 {{ layout }} 台</span>
        <button @click="selected = servers.map((s) => s.id)">全选</button>
        <button @click="selected = []">清空</button>
      </div>

      <div v-if="!servers.length" class="empty">
        还没有服务器。<router-link to="/servers">前往「服务器管理」添加</router-link>
      </div>

      <div v-else class="picker">
        <label v-for="s in servers" :key="s.id" :class="['pick', { on: selected.includes(s.id) }]">
          <input
            type="checkbox"
            :checked="selected.includes(s.id)"
            @change="toggleServer(s.id)"
            style="width:auto"
          />
          <span class="pick-name">{{ s.name }}</span>
          <span class="mono small muted">{{ s.host }}</span>
          <span :class="['badge', s.status === 'online' ? 'ok' : s.status === 'offline' ? 'offline' : 'unknown']">
            {{ s.status }}
          </span>
        </label>
      </div>
    </div>

    <!-- 连接诊断结果 -->
    <div v-if="diagnosis" class="modal-mask" @click.self="diagnosis = null">
      <div class="modal" style="width:640px">
        <h3>连接诊断：{{ diagnosis.name }}</h3>
        <p class="small muted mono" style="margin-top:-8px">{{ diagnosis.target }}</p>
        <div :class="['diag-summary', diagnosis.ok ? 'ok' : 'bad']">
          {{ diagnosis.ok ? '✓ 连接正常' : '✕ 连接存在问题，详见下方各阶段' }}
        </div>
        <pre class="diag-text">{{ diagnosis.text }}</pre>
        <div class="row" style="margin-top:16px;justify-content:flex-end">
          <button @click="diagnosis = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; height: 100%; }
.toolbar { padding: 12px 16px; }

.cmdbar { display: flex; align-items: center; gap: 10px; padding: 10px 14px; }
.cmdbar .prompt { color: var(--ok); font-size: 15px; }
.cmdbar input { flex: 1; }

.grid {
  flex: 1; display: grid; gap: 10px; min-height: 400px;
}
.pane {
  display: flex; flex-direction: column; background: #0b0f14;
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden; min-height: 0;
  position: relative;
}
.pane-head {
  display: flex; align-items: center; gap: 8px; padding: 6px 10px;
  background: var(--bg-soft); border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.pane-title { font-size: 12px; font-weight: 600; }
.term-host { flex: 1; min-height: 0; padding: 6px; }

/* 连接失败时覆盖在终端上方，明确显示原因 */
.pane-error {
  position: absolute; inset: 33px 0 0 0; background: #12181f;
  border-top: 1px solid #3a1a1a; padding: 18px 20px;
  display: flex; flex-direction: column; gap: 10px; align-items: flex-start;
}
.err-title { color: #f87171; font-size: 13px; font-weight: 600; }
.err-msg {
  font-size: 12px; color: var(--text-dim); line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
}

.diag-summary { padding: 10px 14px; border-radius: 7px; font-size: 13px; margin-bottom: 12px; }
.diag-summary.ok { background: #14351f; color: #4ade80; }
.diag-summary.bad { background: #3a1a1a; color: #fca5a5; }
.diag-text {
  background: #0b0f14; padding: 14px; border-radius: 7px; font-size: 12px;
  line-height: 1.8; white-space: pre-wrap; word-break: break-word;
  max-height: 380px; overflow: auto; margin: 0; color: var(--text);
}

.picker { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px; }
.pick {
  display: flex; align-items: center; gap: 9px; padding: 9px 12px;
  border: 1px solid var(--border); border-radius: 7px; cursor: pointer;
  margin: 0; transition: all .15s;
}
.pick:hover { border-color: var(--accent); }
.pick.on { border-color: var(--accent); background: #1b2b45; }
.pick-name { font-size: 13px; color: var(--text); font-weight: 500; }
</style>
