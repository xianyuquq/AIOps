<script setup>
import { onMounted, ref } from 'vue'
import api, { errMsg } from '../api'

const summary = ref(null)
const alerts = ref([])
const loading = ref(true)
const refreshing = ref(false)

const TYPE_LABEL = {
  mysql: 'MySQL / MariaDB',
  postgresql: 'PostgreSQL',
  oracle: 'Oracle',
  kingbase: 'Kingbase 人大金仓'
}

async function load() {
  loading.value = true
  try {
    const [s, a] = await Promise.all([
      api.get('/dashboard/summary'),
      api.get('/alerts', { params: { status: 'firing', limit: 12 } })
    ])
    summary.value = s.data
    alerts.value = a.data
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loading.value = false
  }
}

async function refreshStatus() {
  refreshing.value = true
  try {
    const { data } = await api.post('/servers/refresh-status')
    window.__toast?.(`探测完成：${data.online}/${data.total} 台在线`, 'ok')
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    refreshing.value = false
  }
}

function barColor(v) {
  if (v >= 90) return 'var(--danger)'
  if (v >= 75) return 'var(--warn)'
  return 'var(--ok)'
}

/**
 * 剩余时间越短越紧急：<1 天红、<7 天黄、其余灰。
 * 波动型指标（headroomPct 有值）按「距上限的余量」判断，而不是按时间。
 */
function etaClass(hours, headroomPct) {
  // 波动型：按余量判断
  if (headroomPct !== null && headroomPct !== undefined) {
    if (headroomPct <= 5) return 'critical'
    if (headroomPct <= 15) return 'warning'
    return 'gray'
  }
  if (hours === null || hours === undefined) return 'gray'
  if (hours <= 24) return 'critical'
  if (hours <= 168) return 'warning'
  return 'gray'
}

/** 日均变化是否值得标红：快触顶、或余量很小。 */
function isUrgent(f) {
  if (f.headroom_pct !== null && f.headroom_pct !== undefined) {
    return f.headroom_pct <= 10
  }
  return f.hours_to_limit !== null && f.hours_to_limit !== undefined && f.hours_to_limit < 48
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">总览大盘</h2>
      <div class="spacer"></div>
      <button @click="refreshStatus" :disabled="refreshing">
        {{ refreshing ? '探测中…' : '刷新服务器状态' }}
      </button>
      <button @click="load">重新加载</button>
    </div>

    <div v-if="loading" class="empty">加载中…</div>

    <template v-else-if="summary">
      <div class="stats">
        <div class="card stat">
          <div class="stat-label">服务器在线</div>
          <div class="stat-value">
            {{ summary.server_online }}<span class="muted">/{{ summary.server_total }}</span>
          </div>
        </div>
        <div class="card stat">
          <div class="stat-label">未恢复告警</div>
          <div class="stat-value" :style="{ color: summary.alert_firing ? 'var(--warn)' : 'inherit' }">
            {{ summary.alert_firing }}
          </div>
        </div>
        <div class="card stat">
          <div class="stat-label">严重告警</div>
          <div class="stat-value" :style="{ color: summary.alert_critical ? 'var(--danger)' : 'inherit' }">
            {{ summary.alert_critical }}
          </div>
        </div>
        <div class="card stat">
          <div class="stat-label">启用中脚本</div>
          <div class="stat-value">
            {{ summary.script_enabled }}<span class="muted">/{{ summary.script_total }}</span>
          </div>
        </div>
        <div class="card stat">
          <div class="stat-label">数据库在线</div>
          <div class="stat-value">
            {{ summary.database_online ?? 0 }}<span class="muted">/{{ summary.database_total ?? 0 }}</span>
          </div>
        </div>
      </div>

      <!-- 数据库概况：指标采了就必须看得见 -->
      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <h3 class="card-title" style="margin:0">数据库概况</h3>
          <div class="spacer"></div>
          <router-link to="/databases" class="small">管理实例 →</router-link>
        </div>

        <div v-if="!summary.databases?.length" class="empty small">
          还没有配置数据库实例。前往「数据库监控」添加后即可在此查看连接数、
          缓冲命中率、复制延迟等关键指标。
        </div>

        <table v-else>
          <thead>
            <tr>
              <th>实例</th><th>类型</th><th>状态</th>
              <th>连接数</th><th>命中率</th><th>复制延迟</th><th>锁等待</th><th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in summary.databases" :key="d.id">
              <td><strong>{{ d.name }}</strong></td>
              <td class="small muted">{{ TYPE_LABEL[d.db_type] || d.db_type }}</td>
              <td>
                <span :class="['badge', d.status === 'online' ? 'ok' : d.status === 'offline' ? 'offline' : 'unknown']">
                  {{ d.status === 'online' ? '在线' : d.status === 'offline' ? '异常' : '未知' }}
                </span>
              </td>
              <td>
                <template v-if="d.connections !== null && d.connections !== undefined">
                  <span class="mono">{{ d.connections }}<span class="muted">/{{ d.connections_max ?? '?' }}</span></span>
                  <div class="track mini" v-if="d.connection_usage_pct !== null">
                    <div class="fill" :style="{ width: Math.min(d.connection_usage_pct, 100) + '%', background: barColor(d.connection_usage_pct) }"></div>
                  </div>
                  <span class="small muted">{{ d.connection_usage_pct }}%</span>
                </template>
                <span v-else class="muted small">-</span>
              </td>
              <td class="mono small">
                {{ d.hit_rate !== null && d.hit_rate !== undefined ? d.hit_rate + '%' : '-' }}
              </td>
              <td class="mono small">
                <span v-if="d.lag_sec !== null && d.lag_sec !== undefined"
                      :class="{ warn: d.lag_sec > 10 }">
                  {{ d.lag_sec }}s
                </span>
                <span v-else class="muted">-</span>
              </td>
              <td class="mono small">
                <span v-if="d.locks_waiting !== null && d.locks_waiting !== undefined"
                      :class="{ danger: d.locks_waiting > 0 }">
                  {{ d.locks_waiting }}
                </span>
                <span v-else class="muted">-</span>
              </td>
              <td class="small muted mono">
                {{ d.updated_at ? new Date(d.updated_at).toLocaleTimeString() : '暂无数据' }}
              </td>
            </tr>
          </tbody>
        </table>

        <p v-if="summary.databases?.some((d) => d.last_error)" class="small err-text" style="margin:10px 0 0">
          ⚠ {{ summary.databases.filter((d) => d.last_error).map((d) => `${d.name}: ${d.last_error}`).join('；') }}
        </p>
      </div>

      <!-- 趋势预测：回答「照这个速度还有多久出事」 -->
      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <h3 class="card-title" style="margin:0">趋势预测</h3>
          <span class="small muted" style="margin-left:8px">基于近 24 小时采样做线性拟合</span>
          <div class="spacer"></div>
          <router-link to="/metrics" class="small">查看曲线 →</router-link>
        </div>

        <div v-if="!summary.forecasts?.length" class="empty small">
          暂无可用预测。<br />
          趋势预测需要至少 3 个采样点且指标有持续变化，
          新加入的监控项需要积累一段时间才会出现。
        </div>

        <table v-else>
          <thead>
            <tr>
              <th>对象</th><th>指标</th><th>当前值</th><th>日均变化 / 峰值</th>
              <th>预测</th><th>拟合优度</th><th>可信度</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(f, i) in summary.forecasts" :key="i">
              <td>
                <strong>{{ f.target_name }}</strong>
                <span class="badge gray" style="margin-left:6px">
                  {{ f.target_type === 'database' ? '数据库' : '主机' }}
                </span>
              </td>
              <td class="small">{{ f.label }}</td>
              <td class="mono">{{ f.current }}{{ f.unit }}</td>
              <td class="mono small" :class="{ danger: isUrgent(f) }">
                <template v-if="f.mode === 'peak'">
                  <span class="muted">峰值 </span>{{ f.peak }}
                </template>
                <template v-else>
                  {{ f.slope_per_day > 0 ? '+' : '' }}{{ f.slope_per_day }}{{ f.unit }}/天
                </template>
              </td>
              <td>
                <span v-if="f.eta" :class="['badge', etaClass(f.hours_to_limit, f.headroom_pct)]">
                  {{ f.eta }}
                </span>
                <span v-else-if="f.mode === 'peak'" class="small muted">{{ f.note }}</span>
                <span v-else class="muted small">—</span>
              </td>
              <td class="small muted mono">R²={{ f.r2 }}</td>
              <td>
                <span :class="['badge', f.confidence === 'high' ? 'ok' : f.confidence === 'medium' ? 'warning' : 'gray']">
                  {{ f.confidence === 'high' ? '高' : f.confidence === 'medium' ? '中' : '低' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>

        <p class="small muted" style="margin:10px 0 0">
          「预测」有两类：
          <strong>增长型</strong>（磁盘、内存、库容量）按线性增速推算达到阈值的剩余时间；
          <strong>波动型</strong>（连接数）不做外推，只显示窗口内峰值距上限的余量 ——
          连接数本来就在上限内来回跳，硬做外推会得出「马上耗尽」这种误导结论。
          <br />
          <strong>可信度低</strong>表示样本不足或波动大，此时不给出剩余时间。
        </p>
      </div>

      <div class="grid3">
        <div class="card">
          <h3 class="card-title">CPU 使用率 Top</h3>
          <div v-if="!summary.top_cpu.length" class="empty small">
            暂无数据。请先在「监控脚本」中启用采集脚本。
          </div>
          <div v-for="item in summary.top_cpu" :key="item.server_id" class="metric-row">
            <span class="name">{{ item.server_name }}</span>
            <div class="track"><div class="fill" :style="{ width: Math.min(item.value, 100) + '%', background: barColor(item.value) }"></div></div>
            <span class="mono val">{{ item.value }}%</span>
          </div>
        </div>

        <div class="card">
          <h3 class="card-title">内存使用率 Top</h3>
          <div v-if="!summary.top_memory.length" class="empty small">暂无数据</div>
          <div v-for="item in summary.top_memory" :key="item.server_id" class="metric-row">
            <span class="name">{{ item.server_name }}</span>
            <div class="track"><div class="fill" :style="{ width: Math.min(item.value, 100) + '%', background: barColor(item.value) }"></div></div>
            <span class="mono val">{{ item.value }}%</span>
          </div>
        </div>

        <div class="card">
          <h3 class="card-title">磁盘使用率 Top</h3>
          <div v-if="!summary.top_disk.length" class="empty small">暂无数据</div>
          <div v-for="(item, i) in summary.top_disk" :key="i" class="metric-row">
            <span class="name">{{ item.server_name }}</span>
            <div class="track"><div class="fill" :style="{ width: Math.min(item.value, 100) + '%', background: barColor(item.value) }"></div></div>
            <span class="mono val">{{ item.value }}%</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <h3 class="card-title" style="margin:0">实时告警</h3>
          <div class="spacer"></div>
          <router-link to="/alerts" class="small">查看全部 →</router-link>
        </div>
        <div v-if="!alerts.length" class="empty small">当前没有未恢复的告警</div>
        <table v-else>
          <thead>
            <tr><th>级别</th><th>内容</th><th>时间</th><th>影响</th></tr>
          </thead>
          <tbody>
            <tr v-for="a in alerts" :key="a.id">
              <td><span :class="['badge', a.severity]">{{ a.severity }}</span></td>
              <td>{{ a.title }}</td>
              <td class="small muted mono">{{ new Date(a.fired_at).toLocaleString() }}</td>
              <td class="small">{{ a.affected_count }} 台</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.stat-label { font-size: 12px; color: var(--text-dim); }
.stat-value { font-size: 26px; font-weight: 600; margin-top: 6px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.card-title { font-size: 14px; margin: 0 0 12px; }
.metric-row { display: flex; align-items: center; gap: 10px; padding: 5px 0; }
.name { width: 92px; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.track { flex: 1; height: 7px; background: #2a3444; border-radius: 4px; overflow: hidden; }
.track.mini { height: 4px; width: 70px; margin: 3px 0 1px; }
.fill { height: 100%; border-radius: 4px; transition: width .3s; }
.val { width: 56px; text-align: right; font-size: 12px; }
.warn { color: #fbbf24; }
.danger { color: #f87171; }
.err-text { color: #f87171; }
a { color: var(--accent); text-decoration: none; }
</style>
