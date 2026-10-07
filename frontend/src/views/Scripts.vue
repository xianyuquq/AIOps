<script setup>
import { onMounted, reactive, ref } from 'vue'
import api, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const scripts = ref([])
const loading = ref(true)
const editing = ref(null)
const showEditor = ref(false)
const testResult = ref(null)
const testing = ref(false)

const emptySchema = JSON.stringify(
  { format: 'line_kv', metrics: [{ key: 'my.metric', unit: '', type: 'gauge' }] },
  null,
  2
)

const form = reactive({
  name: '', category: 'custom', runtime: 'shell', platform: 'linux',
  body: '', emit_schema_text: emptySchema, interval_sec: 60, timeout_sec: 15, enabled: true
})

async function load() {
  loading.value = true
  try {
    scripts.value = (await api.get('/scripts')).data
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loading.value = false
  }
}

function openEditor(s) {
  editing.value = s
  testResult.value = null
  Object.assign(form, {
    name: s.name, category: s.category, runtime: s.runtime, platform: s.platform,
    body: s.body, emit_schema_text: JSON.stringify(s.emit_schema || {}, null, 2),
    interval_sec: s.interval_sec, timeout_sec: s.timeout_sec, enabled: s.enabled
  })
  showEditor.value = true
}

async function save() {
  let schema
  try {
    schema = JSON.parse(form.emit_schema_text)
  } catch {
    window.__toast?.('emit_schema 不是合法 JSON', 'err')
    return
  }
  const payload = {
    name: form.name, category: form.category, runtime: form.runtime,
    platform: form.platform, body: form.body, emit_schema: schema,
    interval_sec: Number(form.interval_sec), timeout_sec: Number(form.timeout_sec),
    target_scope: editing.value?.target_scope || { all: true },
    enabled: form.enabled
  }
  try {
    await api.patch(`/scripts/${editing.value.id}`, payload)
    window.__toast?.('已保存', 'ok')
    showEditor.value = false
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    const { data } = await api.post(`/scripts/${editing.value.id}/run`, {})
    testResult.value = data
  } catch (e) {
    testResult.value = { ok: false, error: errMsg(e), raw_output: '', metrics: [] }
  } finally {
    testing.value = false
  }
}

/** 内置脚本只保存运行参数（周期/超时），不触碰采集内容。 */
async function saveBuiltinParams() {
  try {
    await api.patch(`/scripts/${editing.value.id}`, {
      interval_sec: Number(form.interval_sec),
      timeout_sec: Number(form.timeout_sec)
    })
    window.__toast?.('已保存采集参数', 'ok')
    showEditor.value = false
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function toggle(s) {
  try {
    // 只发 enabled 字段：内置脚本允许切换启用状态，但不允许改采集内容，
    // 回传整个对象会被后端的「内容不可修改」校验拦下。
    await api.patch(`/scripts/${s.id}`, { enabled: !s.enabled })
    await load()
    window.__toast?.(s.enabled ? `已停用 ${s.name}` : `已启用 ${s.name}`, 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

/** 复制一份可自由编辑的副本（内置脚本的定制入口）。 */
async function duplicate(s) {
  if (!confirm(`复制「${s.name}」为一份可编辑的副本？`)) return
  try {
    const { data } = await api.post(`/scripts/${s.id}/duplicate`)
    window.__toast?.(`已创建副本：${data.name}`, 'ok')
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

/** 修改采集周期（内置脚本允许调整运行参数）。 */
async function saveInterval(s) {
  try {
    await api.patch(`/scripts/${s.id}`, {
      interval_sec: Number(s.interval_sec),
      timeout_sec: Number(s.timeout_sec)
    })
    window.__toast?.('已更新采集参数', 'ok')
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">监控脚本</h2>
      <div class="spacer"></div>
      <button @click="api.post('/collector/reload').then(() => window.__toast?.('已重载采集任务', 'ok'))">
        重载采集任务
      </button>
    </div>

    <div class="card small muted">
      脚本是可插拔的监控扩展点：支持 <strong>shell</strong>（在目标服务器执行）、
      <strong>python</strong>（平台本地沙箱执行）与 <strong>sql</strong>（数据库采集）。
      输出采用行式键值格式，便于用现成的 shell 命令接入。
    </div>

    <div class="card">
      <div v-if="loading" class="empty">加载中…</div>
      <table v-else>
        <thead>
          <tr><th>名称</th><th>类型</th><th>运行时</th><th>平台</th><th>周期</th><th>状态</th><th style="width:170px">操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in scripts" :key="s.id">
            <td>
              <strong>{{ s.name }}</strong>
              <span v-if="s.is_builtin" class="badge gray" style="margin-left:6px">内置</span>
            </td>
            <td class="small">{{ s.category }}</td>
            <td class="small mono">{{ s.runtime }}</td>
            <td class="small">{{ s.platform }}</td>
            <td class="small muted">{{ s.interval_sec }}s</td>
            <td>
              <span :class="['badge', s.enabled ? 'ok' : 'gray']">
                {{ s.enabled ? '已启用' : '已停用' }}
              </span>
            </td>
            <td>
              <button class="sm" :disabled="!auth.canOperate" @click="openEditor(s)">查看/编辑</button>
              <button class="sm primary" style="margin-left:4px" :disabled="!auth.canOperate" @click="toggle(s)">
                {{ s.enabled ? '停用' : '启用' }}
              </button>
              <button class="sm" style="margin-left:4px" :disabled="!auth.canOperate" @click="duplicate(s)">
                复制
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="showEditor" class="modal-mask" @click.self="showEditor = false">
      <div class="modal" style="width:920px;max-width:94vw">
        <h3>{{ editing.name }}<span v-if="editing.is_builtin" class="badge gray" style="margin-left:8px">内置脚本</span></h3>

        <div v-if="editing.is_builtin" class="builtin-note">
          内置脚本的<strong>采集内容</strong>（脚本正文与解析规则）不可修改，
          但可以<strong>启用/停用</strong>、<strong>调整采集周期</strong>与<strong>试运行</strong>。
          如需定制内容，请关闭本窗口后点击列表中的「复制」生成副本。
        </div>

        <div class="row" style="gap:14px;margin-bottom:12px">
          <div style="flex:1"><label>名称</label><input v-model="form.name" :disabled="editing.is_builtin" /></div>
          <div style="width:120px">
            <label>运行时</label>
            <select v-model="form.runtime" :disabled="editing.is_builtin">
              <option value="shell">shell</option>
              <option value="python">python</option>
              <option value="sql">sql</option>
            </select>
          </div>
          <div style="width:120px">
            <label>平台</label>
            <select v-model="form.platform" :disabled="editing.is_builtin">
              <option value="linux">linux</option>
              <option value="windows">windows</option>
              <option value="any">any</option>
            </select>
          </div>
          <div style="width:100px"><label>周期(s)</label><input v-model.number="form.interval_sec" type="number" /></div>
          <div style="width:100px"><label>超时(s)</label><input v-model.number="form.timeout_sec" type="number" /></div>
        </div>

        <label>脚本内容</label>
        <textarea v-model="form.body" rows="14" spellcheck="false" :readonly="editing.is_builtin"></textarea>

        <label style="margin-top:14px">emit_schema（声明输出格式与指标）</label>
        <textarea v-model="form.emit_schema_text" rows="8" spellcheck="false" :readonly="editing.is_builtin"></textarea>

        <div class="row" style="margin-top:18px">
          <button @click="runTest" :disabled="testing">
            {{ testing ? '试运行中…' : '试运行（在目标服务器）' }}
          </button>
          <span class="small muted">试运行不会写入数据库，可安全验证解析是否正确</span>
          <div class="spacer"></div>
          <button @click="showEditor = false">关闭</button>
          <button
            v-if="editing.is_builtin"
            class="primary"
            @click="saveBuiltinParams"
          >
            保存采集参数
          </button>
          <button v-else class="primary" @click="save">保存</button>
        </div>

        <div v-if="testResult" class="test-result">
          <div class="row" style="margin-bottom:8px">
            <span :class="['badge', testResult.ok ? 'ok' : 'offline']">
              {{ testResult.ok ? '成功' : '失败' }}
            </span>
            <span class="small muted">耗时 {{ testResult.duration_ms }}ms</span>
            <span v-if="testResult.truncated" class="badge warning">输出被截断</span>
          </div>
          <p v-if="testResult.risk_warning" class="warn-text small">⚠ {{ testResult.risk_warning }}</p>
          <p v-if="testResult.error" class="err-text small mono">{{ testResult.error }}</p>

          <div v-if="testResult.metrics?.length" style="margin-top:10px">
            <div class="small muted" style="margin-bottom:6px">
              解析出 {{ testResult.metrics.length }} 个指标
            </div>
            <table>
              <thead><tr><th>指标</th><th>值</th><th>标签</th></tr></thead>
              <tbody>
                <tr v-for="(m, i) in testResult.metrics.slice(0, 30)" :key="i">
                  <td class="mono small">{{ m.key }}</td>
                  <td class="mono small">{{ m.value }}</td>
                  <td class="mono small muted">{{ JSON.stringify(m.labels) }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <details style="margin-top:10px">
            <summary class="small muted" style="cursor:pointer">查看原始输出</summary>
            <pre class="raw">{{ testResult.raw_output }}</pre>
          </details>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.test-result {
  margin-top:16px; padding:14px; background: var(--bg-soft);
  border: 1px solid var(--border); border-radius: 8px;
}
.raw {
  max-height: 240px; overflow: auto; font-size: 11px;
  background: #0b0f14; padding: 10px; border-radius: 6px; margin: 8px 0 0;
  white-space: pre-wrap; word-break: break-all;
}
.err-text { color: #f87171; }
.warn-text { color: #fbbf24; }
.builtin-note {
  background: #1e2a3d; border-left: 3px solid var(--accent);
  padding: 10px 14px; border-radius: 0 6px 6px 0;
  font-size: 12px; line-height: 1.75; margin-bottom: 14px; color: var(--text-dim);
}
.builtin-note strong { color: var(--text); }
textarea[readonly] { opacity: .75; cursor: default; }
</style>
