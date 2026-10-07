<script setup>
import { onMounted, ref } from 'vue'
import api, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const alerts = ref([])
const rules = ref([])
const tab = ref('events')
const statusFilter = ref('firing')
const loading = ref(true)
const analyzing = ref({})
const detail = ref(null)

async function load() {
  loading.value = true
  try {
    const [a, r] = await Promise.all([
      api.get('/alerts', { params: { status: statusFilter.value, limit: 200 } }),
      api.get('/alerts/rules')
    ])
    alerts.value = a.data
    rules.value = r.data
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loading.value = false
  }
}

async function analyze(a) {
  analyzing.value = { ...analyzing.value, [a.id]: true }
  try {
    const { data } = await api.post(`/alerts/${a.id}/analyze`)
    detail.value = { alert: a, analysis: data }
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    analyzing.value = { ...analyzing.value, [a.id]: false }
  }
}

async function acknowledge(a) {
  try {
    await api.post(`/alerts/${a.id}/acknowledge`)
    window.__toast?.('已确认，进入静默窗口', 'ok')
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function toggleRule(r) {
  try {
    await api.patch(`/alerts/rules/${r.id}`, { ...r, enabled: !r.enabled })
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

function copyCommand(cmd) {
  navigator.clipboard?.writeText(cmd)
  window.__toast?.('命令已复制', 'ok')
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">告警中心</h2>
      <div class="spacer"></div>
      <div class="tabs">
        <button :class="['tab', { on: tab === 'events' }]" @click="tab = 'events'">告警事件</button>
        <button :class="['tab', { on: tab === 'rules' }]" @click="tab = 'rules'">告警规则</button>
      </div>
      <button @click="load">刷新</button>
    </div>

    <!-- 告警事件 -->
    <template v-if="tab === 'events'">
      <div class="card row" style="padding:10px 14px">
        <label style="margin:0">状态</label>
        <select v-model="statusFilter" style="width:150px" @change="load">
          <option value="firing">未恢复</option>
          <option value="acknowledged">已确认</option>
          <option value="resolved">已恢复</option>
          <option value="all">全部</option>
        </select>
        <div class="spacer"></div>
        <span class="muted small">共 {{ alerts.length }} 条</span>
      </div>

      <div v-if="loading" class="card empty">加载中…</div>
      <div v-else-if="!alerts.length" class="card empty">没有符合条件的告警</div>

      <div v-for="a in alerts" :key="a.id" class="card alert-card">
        <div class="row">
          <span :class="['badge', a.severity]">{{ a.severity }}</span>
          <strong style="font-size:13px">{{ a.title }}</strong>
          <div class="spacer"></div>
          <span v-if="a.affected_count > 1" class="badge warning">
            聚合 {{ a.affected_count }} 台
          </span>
          <span class="small muted mono">{{ new Date(a.fired_at).toLocaleString() }}</span>
        </div>

        <div class="row small muted" style="margin-top:6px;gap:14px">
          <span>服务器：{{ a.server_name || '-' }}</span>
          <span>指标：{{ a.detail?.metric_key }}</span>
          <span>当前值：{{ a.detail?.value }}</span>
          <span>阈值：{{ a.detail?.operator }} {{ a.detail?.threshold }}</span>
          <span v-if="a.detail?.labels && Object.keys(a.detail.labels).length">
            标签：{{ JSON.stringify(a.detail.labels) }}
          </span>
        </div>

        <div v-if="a.detail?.affected_servers?.length > 1" class="small muted" style="margin-top:6px">
          受影响：{{ a.detail.affected_servers.join('、') }}
        </div>

        <div v-if="a.ai_analysis?.summary" class="ai-box">
          <div class="row" style="margin-bottom:4px">
            <span class="badge info">AI 分析</span>
            <span v-if="a.ai_analysis.ai_enabled === false" class="badge gray">规则降级</span>
            <span class="small muted">置信度 {{ a.ai_analysis.confidence ?? '-' }}</span>
          </div>
          <p style="margin:4px 0">{{ a.ai_analysis.summary }}</p>
        </div>

        <div class="row" style="margin-top:10px">
          <button class="sm" :disabled="analyzing[a.id]" @click="analyze(a)">
            {{ analyzing[a.id] ? '分析中…' : 'AI 根因分析' }}
          </button>
          <button class="sm" :disabled="!auth.canOperate || a.status !== 'firing'" @click="acknowledge(a)">
            确认告警
          </button>
        </div>
      </div>
    </template>

    <!-- 告警规则 -->
    <div v-else class="card">
      <table>
        <thead>
          <tr><th>规则</th><th>指标</th><th>条件</th><th>持续</th><th>级别</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in rules" :key="r.id">
            <td>{{ r.name }}</td>
            <td class="mono small">{{ r.metric_key }}</td>
            <td class="mono small">{{ r.operator }} {{ r.threshold }}</td>
            <td class="small muted">{{ r.duration_sec }}s</td>
            <td><span :class="['badge', r.severity]">{{ r.severity }}</span></td>
            <td><span :class="['badge', r.enabled ? 'ok' : 'gray']">{{ r.enabled ? '启用' : '停用' }}</span></td>
            <td>
              <button class="sm" :disabled="!auth.canOperate" @click="toggleRule(r)">
                {{ r.enabled ? '停用' : '启用' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 根因分析详情 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal" style="width:760px">
        <h3>根因分析</h3>
        <p class="small muted" style="margin-top:-8px">{{ detail.alert.title }}</p>

        <div class="ai-box" style="margin-top:12px">
          <p style="margin:0 0 8px">{{ detail.analysis.summary }}</p>
          <div class="row small muted">
            <span>严重程度：{{ detail.analysis.severity_assessment }}</span>
            <span>置信度：{{ detail.analysis.confidence }}</span>
            <span v-if="detail.analysis.model">模型：{{ detail.analysis.model }}</span>
            <span v-if="detail.analysis.ai_enabled === false" class="badge gray">AI 未启用，规则降级</span>
          </div>
        </div>

        <h4 class="sec">可能根因</h4>
        <div v-for="(c, i) in detail.analysis.root_causes || []" :key="i" class="cause">
          <div class="row">
            <strong style="font-size:13px">{{ c.cause }}</strong>
            <div class="spacer"></div>
            <span class="badge gray">置信度 {{ c.confidence }}</span>
          </div>
          <ul class="small muted" style="margin:6px 0 0 18px">
            <li v-for="(e, j) in c.evidence || []" :key="j">{{ e }}</li>
          </ul>
        </div>

        <h4 class="sec">建议操作</h4>
        <p class="small warn-text" style="margin:0 0 8px">
          ⚠ AI 只提供建议，不会自动执行。请在确认风险后手动操作。
        </p>
        <div v-for="s in detail.analysis.suggested_actions || []" :key="s.step" class="action">
          <div class="row">
            <span class="step-no">{{ s.step }}</span>
            <span style="font-size:13px">{{ s.action }}</span>
            <div class="spacer"></div>
            <span :class="['badge', s.risk === 'high' ? 'critical' : s.risk === 'medium' ? 'warning' : 'ok']">
              {{ s.risk }}
            </span>
          </div>
          <div v-if="s.command" class="cmd-row">
            <code class="mono">{{ s.command }}</code>
            <button class="sm" @click="copyCommand(s.command)">复制</button>
          </div>
        </div>

        <div class="row" style="margin-top:18px;justify-content:flex-end">
          <button @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tabs { display: flex; gap: 4px; }
.tab { background: transparent; border-color: transparent; color: var(--text-dim); }
.tab.on { background: var(--bg-panel); border-color: var(--border); color: var(--text); }
.alert-card { padding: 14px 16px; }
.ai-box {
  margin-top: 10px; padding: 10px 12px; background: #16243a;
  border-left: 3px solid var(--accent); border-radius: 0 6px 6px 0;
}
.sec { font-size: 13px; margin: 18px 0 8px; color: var(--text-dim); }
.cause { padding: 10px 12px; background: var(--bg-soft); border-radius: 6px; margin-bottom: 8px; }
.action { padding: 10px 12px; background: var(--bg-soft); border-radius: 6px; margin-bottom: 8px; }
.step-no {
  width: 20px; height: 20px; border-radius: 50%; background: var(--accent);
  color: #fff; font-size: 11px; display: flex; align-items: center; justify-content: center;
}
.cmd-row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
.cmd-row code {
  flex: 1; background: #0b0f14; padding: 7px 10px; border-radius: 5px;
  font-size: 11px; overflow-x: auto; white-space: nowrap;
}
.warn-text { color: #fbbf24; }
</style>
