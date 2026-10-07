<script setup>
import { onMounted, reactive, ref } from 'vue'
import api, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const instances = ref([])
const servers = ref([])
const dbTypes = ref([])
const showForm = ref(false)
const editing = ref(null)
const testing = ref({})
const collecting = ref({})
const detail = ref(null)

const TYPE_LABEL = {
  mysql: 'MySQL / MariaDB',
  postgresql: 'PostgreSQL',
  oracle: 'Oracle',
  kingbase: 'Kingbase 人大金仓'
}

const blank = () => ({
  name: '', db_type: 'mysql', host: '', port: 3306, username: '',
  secret: '', database_name: '', collect_mode: 'direct',
  proxy_server_id: null, proxy_host: '', client_path: '', enabled: true
})
const form = reactive(blank())

async function load() {
  try {
    const [d, s, t, o] = await Promise.all([
      api.get('/databases'),
      api.get('/servers'),
      api.get('/databases/templates'),
      api.get('/databases/overview')
    ])
    instances.value = d.data
    servers.value = s.data
    dbTypes.value = t.data.db_types || []
    // 把实时指标挂到实例上，列表直接能看到数据库当前状况
    const byId = Object.fromEntries((o.data.databases || []).map((x) => [x.id, x]))
    instances.value = d.data.map((x) => ({ ...x, live: byId[x.id] || null }))
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

function defaultPort(type) {
  return dbTypes.value.find((x) => x.key === type)?.default_port || 5432
}

function onTypeChange() {
  form.port = defaultPort(form.db_type)
}

function openCreate() {
  Object.assign(form, blank())
  form.port = defaultPort('mysql')
  editing.value = null
  showForm.value = true
}

function openEdit(item) {
  Object.assign(form, {
    name: item.name, db_type: item.db_type, host: item.host, port: item.port,
    username: item.username, secret: '', database_name: item.database_name,
    collect_mode: item.collect_mode, proxy_server_id: item.proxy_server_id,
    proxy_host: item.proxy_host || '', client_path: item.client_path || '',
    enabled: item.enabled
  })
  editing.value = item
  showForm.value = true
}

async function save() {
  if (!form.name || !form.host) {
    window.__toast?.('名称与主机必填', 'warn')
    return
  }
  const payload = { ...form, port: Number(form.port) }
  // 编辑时留空密码表示不修改
  if (editing.value && !payload.secret) delete payload.secret
  try {
    if (editing.value) await api.patch(`/databases/${editing.value.id}`, payload)
    else await api.post('/databases', payload)
    window.__toast?.(editing.value ? '已更新' : '已添加', 'ok')
    showForm.value = false
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function test(item) {
  testing.value = { ...testing.value, [item.id]: true }
  try {
    const { data } = await api.post(`/databases/${item.id}/test`)
    window.__toast?.(
      `${item.name}：${data.message}${data.duration_ms ? ` (${data.duration_ms}ms)` : ''}`,
      data.ok ? 'ok' : 'err'
    )
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    testing.value = { ...testing.value, [item.id]: false }
  }
}

async function collect(item) {
  collecting.value = { ...collecting.value, [item.id]: true }
  try {
    const { data } = await api.post(`/databases/${item.id}/collect`)
    detail.value = {
      name: item.name,
      ok: data.ok,
      stored: data.stored || 0,
      results: data.results || []
    }
    window.__toast?.(
      data.ok ? `采集完成，写入 ${data.stored || 0} 个指标` : '采集失败',
      data.ok ? 'ok' : 'err'
    )
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    collecting.value = { ...collecting.value, [item.id]: false }
  }
}

async function remove(item) {
  if (!confirm(`确定删除「${item.name}」？其关联的备份任务也会一并删除。`)) return
  try {
    await api.delete(`/databases/${item.id}`)
    await load()
    window.__toast?.('已删除', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">数据库监控</h2>
      <div class="spacer"></div>
      <button @click="load">刷新</button>
      <button class="primary" :disabled="!auth.canOperate" @click="openCreate">添加实例</button>
    </div>

    <div class="card small muted">
      支持 <strong>MySQL / MariaDB</strong>、<strong>PostgreSQL</strong>、
      <strong>Oracle</strong> 与 <strong>Kingbase（人大金仓）</strong>。
      采集方式可选 <strong>直连</strong>（平台用驱动直接连接）或
      <strong>SSH 跳板</strong>（通过已纳管服务器执行数据库客户端命令，
      适用于数据库不对外暴露端口的场景）。
    </div>

    <div class="card">
      <div v-if="!instances.length" class="empty">
        还没有配置数据库实例。点击右上角「添加实例」开始。
      </div>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th><th>类型</th><th>地址</th><th>库名</th>
            <th>采集方式</th><th>凭据</th><th>状态</th><th style="width:230px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in instances" :key="d.id">
            <td><strong>{{ d.name }}</strong></td>
            <td class="small">{{ TYPE_LABEL[d.db_type] || d.db_type }}</td>
            <td class="mono small">{{ d.host }}:{{ d.port }}</td>
            <td class="small mono muted">{{ d.database_name || '-' }}</td>
            <td class="small">
              <span v-if="d.collect_mode === 'direct'">直连</span>
              <span v-else :title="`跳板内连接地址：${d.proxy_host || d.host}`">
                跳板{{ d.proxy_host ? ` → ${d.proxy_host}` : '' }}
              </span>
            </td>
            <td>
              <span v-if="d.has_secret" class="badge ok">已保存</span>
              <span v-else class="badge offline" title="没有保存密码，无法认证">未保存</span>
            </td>
            <td>
              <span :class="['badge', d.status === 'online' ? 'ok' : d.status === 'offline' ? 'offline' : 'unknown']">
                {{ d.status === 'online' ? '在线' : d.status === 'offline' ? '异常' : '未知' }}
              </span>
              <div v-if="d.live" class="live-mini small muted">
                <span v-if="d.live.connections !== null && d.live.connections !== undefined">
                  连接 {{ d.live.connections }}<template v-if="d.live.connections_max">/{{ d.live.connections_max }}</template>
                </span>
                <span v-if="d.live.hit_rate !== null && d.live.hit_rate !== undefined">
                  命中 {{ d.live.hit_rate }}%
                </span>
                <span v-if="d.live.lag_sec" :class="{ warn: d.live.lag_sec > 10 }">
                  延迟 {{ d.live.lag_sec }}s
                </span>
                <span v-if="d.live.locks_waiting" class="danger">锁 {{ d.live.locks_waiting }}</span>
              </div>
              <div v-else class="small muted">暂无指标</div>
            </td>
            <td>
              <button class="sm" :disabled="testing[d.id]" @click="test(d)">
                {{ testing[d.id] ? '测试中' : '测试连接' }}
              </button>
              <button class="sm" style="margin-left:4px" :disabled="collecting[d.id] || !auth.canOperate" @click="collect(d)">
                {{ collecting[d.id] ? '采集中' : '立即采集' }}
              </button>
              <button class="sm" style="margin-left:4px" :disabled="!auth.canOperate" @click="openEdit(d)">编辑</button>
              <button class="sm danger" style="margin-left:4px" :disabled="!auth.canOperate" @click="remove(d)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-for="d in instances.filter((x) => x.last_error)" :key="'e' + d.id" class="err-row">
        <span class="badge offline">{{ d.name }}</span>
        <span class="small mono">{{ d.last_error }}</span>
      </div>
    </div>

    <div class="notice card small">
      <strong>关于 Kingbase：</strong>
      人大金仓兼容 PostgreSQL 协议（因此复用同一驱动），但系统视图前缀是
      <code class="mono">sys_</code> 而非 <code class="mono">pg_</code>
      （如 <code class="mono">sys_stat_activity</code>），所以采集 SQL 是独立的一份。
    </div>

    <!-- 新增/编辑 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="modal">
        <h3>{{ editing ? '编辑数据库实例' : '添加数据库实例' }}</h3>
        <div class="grid2">
          <div><label>名称 *</label><input v-model="form.name" placeholder="生产库-主" /></div>
          <div>
            <label>类型</label>
            <select v-model="form.db_type" @change="onTypeChange">
              <option v-for="t in dbTypes" :key="t.key" :value="t.key">{{ t.label }}</option>
            </select>
          </div>
          <div><label>主机 *</label><input v-model="form.host" placeholder="192.168.1.20" /></div>
          <div><label>端口</label><input v-model.number="form.port" type="number" /></div>
          <div><label>用户名</label><input v-model="form.username" /></div>
          <div>
            <label>
              密码
              <span v-if="editing" class="muted">（留空表示不修改）</span>
            </label>
            <input v-model="form.secret" type="password" :placeholder="editing ? '留空保持原密码' : ''" />
          </div>
          <div>
            <label>库名 / 服务名</label>
            <input v-model="form.database_name" placeholder="testdb 或 ORCL" />
          </div>
          <div>
            <label>采集方式</label>
            <select v-model="form.collect_mode">
              <option value="direct">直连数据库</option>
              <option value="ssh_proxy">SSH 跳板</option>
            </select>
          </div>
          <div v-if="form.collect_mode === 'ssh_proxy'" style="grid-column:1/-1">
            <label>跳板服务器 *</label>
            <select v-model="form.proxy_server_id">
              <option :value="null">请选择</option>
              <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.host }})</option>
            </select>
          </div>
          <template v-if="form.collect_mode === 'ssh_proxy'">
            <div>
              <label>
                从跳板机连接的地址
                <span class="muted">（留空用上面的主机）</span>
              </label>
              <input v-model="form.proxy_host" class="mono" placeholder="127.0.0.1" />
            </div>
            <div>
              <label>
                客户端路径
                <span class="muted">（不在 PATH 中时必填）</span>
              </label>
              <input
                v-model="form.client_path"
                class="mono"
                :placeholder="form.db_type === 'kingbase' ? '/data/Kingbase/ES/V9/KESRealPro/*/Server/bin/ksql' : '（留空自动查找）'"
              />
            </div>
            <div style="grid-column:1/-1" class="hint-box small">
              <strong>提示</strong>：数据库常常只监听本机或内网，防火墙也只放行跳板机。
              如果直接连不通，把「从跳板机连接的地址」填成 <code class="mono">127.0.0.1</code>；
              若日志提示找不到 <code class="mono">ksql</code> / <code class="mono">psql</code> /
              <code class="mono">mysql</code>，请填写客户端可执行文件的完整路径。
            </div>
          </template>
          <div style="grid-column:1/-1">
            <label class="row" style="gap:6px;margin:0">
              <input type="checkbox" v-model="form.enabled" style="width:auto" />
              <span style="color:var(--text)">启用该实例的定时采集</span>
            </label>
          </div>
        </div>
        <div class="row" style="margin-top:20px;justify-content:flex-end">
          <button @click="showForm = false">取消</button>
          <button class="primary" @click="save">保存</button>
        </div>
      </div>
    </div>

    <!-- 采集结果 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal" style="width:720px">
        <h3>采集结果：{{ detail.name }}</h3>
        <div :class="['diag-summary', detail.ok ? 'ok' : 'bad']">
          {{ detail.ok ? `✓ 采集成功，写入 ${detail.stored} 个指标` : '✕ 采集失败' }}
        </div>
        <div v-for="(r, i) in detail.results" :key="i" class="script-result">
          <div class="row">
            <span :class="['badge', r.ok ? 'ok' : 'offline']">{{ r.ok ? '成功' : '失败' }}</span>
            <strong style="font-size:13px">{{ r.script }}</strong>
            <div class="spacer"></div>
            <span class="small muted">{{ r.duration_ms }}ms</span>
          </div>
          <p v-if="r.error" class="err-text small mono" style="margin:8px 0 0">{{ r.error }}</p>
          <div v-if="r.metrics?.length" class="metric-grid">
            <div v-for="(m, j) in r.metrics" :key="j" class="metric-item">
              <span class="mono small">{{ m.key }}</span>
              <span class="mono small val">{{ m.value }}</span>
            </div>
          </div>
        </div>
        <div class="row" style="margin-top:16px;justify-content:flex-end">
          <button @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.notice { border-left: 3px solid var(--warn); line-height: 1.7; }
.err-row { display: flex; gap: 10px; align-items: center; padding: 6px 0; }
code { background: var(--bg-soft); padding: 1px 5px; border-radius: 4px; }
.diag-summary { padding: 10px 14px; border-radius: 7px; font-size: 13px; margin-bottom: 12px; }
.diag-summary.ok { background: #14351f; color: #4ade80; }
.diag-summary.bad { background: #3a1a1a; color: #fca5a5; }
.script-result { padding: 12px; background: var(--bg-soft); border-radius: 7px; margin-bottom: 10px; }
.metric-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 4px 14px; margin-top: 10px;
}
.metric-item { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; }
.metric-item .val { color: var(--accent); }
.err-text { color: #f87171; }
.live-mini { display: flex; flex-direction: column; gap: 1px; margin-top: 4px; line-height: 1.5; }
.live-mini .warn { color: #fbbf24; }
.live-mini .danger { color: #f87171; }
.hint-box {
  background: #1e2a3d; border-left: 3px solid var(--accent);
  padding: 10px 14px; border-radius: 0 6px 6px 0;
  line-height: 1.75; color: var(--text-dim);
}
.hint-box strong { color: var(--text); }
</style>
