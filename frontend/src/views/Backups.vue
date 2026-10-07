<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import api, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const tasks = ref([])
const templates = ref([])
const instances = ref([])
const servers = ref([])
const showForm = ref(false)
const editing = ref(null)
const running = ref({})
const history = ref(null)

const blank = () => ({
  name: '', db_instance_id: null, server_id: null, exec_server_id: null,
  backup_type: 'logical', template_key: '', script: '',
  target_dir: '/var/backups/aiops', compress: true,
  schedule_cron: '0 2 * * *', retention_days: 7, timeout_sec: 1800, enabled: false
})
const form = reactive(blank())

// 占位符列表放在数据里：模板里直接写 {{host}} 会被 Vue 当作插值解析而报错
const placeholders = [
  '{{host}}', '{{port}}', '{{user}}', '{{database}}',
  '{{dir}}', '{{name}}', '{{stamp}}', '{{retention}}'
]

const groupedTemplates = computed(() => {
  const groups = {}
  for (const t of templates.value) {
    ;(groups[t.group] = groups[t.group] || []).push(t)
  }
  return groups
})

async function load() {
  try {
    const [t, tpl, d, s] = await Promise.all([
      api.get('/backups'),
      api.get('/backups/templates'),
      api.get('/databases'),
      api.get('/servers')
    ])
    tasks.value = t.data
    templates.value = tpl.data.templates || []
    instances.value = d.data
    servers.value = s.data
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

/** 选择模板：填充脚本与默认目录、周期。 */
function applyTemplate(key) {
  const tpl = templates.value.find((t) => t.key === key)
  if (!tpl) return
  form.template_key = key
  form.script = tpl.script
  form.backup_type = tpl.backup_type
  form.target_dir = tpl.target_dir
  if (tpl.schedule_cron) form.schedule_cron = tpl.schedule_cron
  // 自动匹配同类型的数据库实例
  const match = instances.value.find((d) => d.db_type === tpl.db_type)
  if (match && !form.db_instance_id) {
    form.db_instance_id = match.id
    if (!form.name) form.name = `${match.name} 备份`
    if (match.proxy_server_id && !form.exec_server_id) {
      form.exec_server_id = match.proxy_server_id
    }
  }
}

function openCreate() {
  Object.assign(form, blank())
  editing.value = null
  showForm.value = true
}

function openEdit(t) {
  Object.assign(form, {
    name: t.name, db_instance_id: t.db_instance_id, server_id: t.server_id,
    exec_server_id: t.exec_server_id, backup_type: t.backup_type,
    template_key: t.template_key, script: t.script, target_dir: t.target_dir,
    compress: t.compress, schedule_cron: t.schedule_cron,
    retention_days: t.retention_days, timeout_sec: t.timeout_sec, enabled: t.enabled
  })
  editing.value = t
  showForm.value = true
}

async function save() {
  if (!form.name || !form.script) {
    window.__toast?.('名称与备份命令必填', 'warn')
    return
  }
  if (!form.exec_server_id) {
    window.__toast?.('请选择「执行服务器」：备份命令需要在能访问数据库的机器上运行', 'warn')
    return
  }
  try {
    if (editing.value) await api.patch(`/backups/${editing.value.id}`, form)
    else await api.post('/backups', form)
    window.__toast?.(editing.value ? '已更新' : '已创建', 'ok')
    showForm.value = false
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function runNow(t) {
  running.value = { ...running.value, [t.id]: true }
  try {
    const { data } = await api.post(`/backups/${t.id}/run`)
    window.__toast?.(
      data.ok ? `备份成功（${data.duration_ms}ms）` : `备份失败：${(data.error || '').slice(0, 80)}`,
      data.ok ? 'ok' : 'err'
    )
    await load()
    await showHistory(t, true)
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    running.value = { ...running.value, [t.id]: false }
  }
}

async function toggle(t) {
  try {
    await api.patch(`/backups/${t.id}`, { enabled: !t.enabled })
    await load()
    window.__toast?.(t.enabled ? '已停用调度' : '已启用调度', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function showHistory(t, silent = false) {
  try {
    const { data } = await api.get(`/backups/${t.id}/runs`)
    history.value = { task: t, runs: data.runs || [] }
    if (!silent && !data.runs?.length) window.__toast?.('该任务还没有执行记录')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function remove(t) {
  if (!confirm(`确定删除备份任务「${t.name}」？执行历史也会删除。`)) return
  try {
    await api.delete(`/backups/${t.id}`)
    await load()
    window.__toast?.('已删除', 'ok')
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

function cronText(expr) {
  if (!expr) return '未设置'
  const map = {
    '0 2 * * *': '每天 02:00',
    '0 3 * * 0': '每周日 03:00',
    '30 1 * * *': '每天 01:30',
    '0 * * * *': '每小时'
  }
  return map[expr] || expr
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">备份与容灾</h2>
      <div class="spacer"></div>
      <button @click="load">刷新</button>
      <button class="primary" :disabled="!auth.canOperate" @click="openCreate">新建备份任务</button>
    </div>

    <div class="card small muted">
      从内置模板快速创建备份任务，也可自行编写命令。
      <br />
      备份命令通过 SSH 在<strong>执行服务器</strong>上运行（通常是数据库所在主机，
      因为 <code class="mono">mysqldump</code> / <code class="mono">pg_dump</code>
      需要在能访问数据库且装有客户端工具的机器上执行）。
      <br />
      <strong>安全设计</strong>：数据库密码<strong>不写入脚本</strong>，运行时通过
      <code class="mono">MYSQL_PWD</code> / <code class="mono">PGPASSWORD</code>
      环境变量注入，因此不会出现在进程列表（<code class="mono">ps</code>）与日志中。
      <br />
      模板末尾自带<strong>保留期清理</strong>，避免备份文件把磁盘撑满。
    </div>

    <div v-if="!tasks.length" class="card empty">
      还没有备份任务。点击「新建备份任务」，从模板中选择一种即可。
    </div>

    <div v-for="t in tasks" :key="t.id" class="card task">
      <div class="row">
        <span :class="['badge', t.enabled ? 'ok' : 'gray']">
          {{ t.enabled ? '已调度' : '未调度' }}
        </span>
        <strong style="font-size:14px">{{ t.name }}</strong>
        <span class="badge gray">{{ t.backup_type === 'logical' ? '逻辑备份' : t.backup_type === 'config' ? '配置备份' : '物理备份' }}</span>
        <div class="spacer"></div>
        <span
          v-if="t.last_status"
          :class="['badge', t.last_status === 'success' ? 'ok' : 'offline']"
        >
          上次：{{ t.last_status === 'success' ? '成功' : '失败' }}
        </span>
        <span class="small muted">{{ t.last_run_at ? new Date(t.last_run_at).toLocaleString() : '尚未执行' }}</span>
      </div>

      <div class="row small muted" style="margin-top:8px;gap:16px;flex-wrap:wrap">
        <span>目标：{{ t.db_instance_name || '未关联数据库' }}</span>
        <span>执行主机：{{ t.exec_server_name || '未指定' }}</span>
        <span>目录：<code class="mono">{{ t.target_dir }}</code></span>
        <span>周期：{{ cronText(t.schedule_cron) }}</span>
        <span>保留：{{ t.retention_days }} 天</span>
      </div>

      <p v-if="t.last_message" class="small" :class="t.last_status === 'success' ? 'muted' : 'err-text'"
         style="margin:8px 0 0">
        {{ t.last_message }}
      </p>

      <div class="row" style="margin-top:10px">
        <button class="sm primary" :disabled="running[t.id] || !auth.canOperate" @click="runNow(t)">
          {{ running[t.id] ? '执行中…' : '立即备份' }}
        </button>
        <button class="sm" style="margin-left:4px" @click="showHistory(t)">执行历史</button>
        <button class="sm" style="margin-left:4px" :disabled="!auth.canOperate" @click="toggle(t)">
          {{ t.enabled ? '停用调度' : '启用调度' }}
        </button>
        <button class="sm" style="margin-left:4px" :disabled="!auth.canOperate" @click="openEdit(t)">编辑</button>
        <button class="sm danger" style="margin-left:4px" :disabled="!auth.canOperate" @click="remove(t)">删除</button>
      </div>
    </div>

    <!-- 新建/编辑 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="modal" style="width:880px;max-width:94vw">
        <h3>{{ editing ? '编辑备份任务' : '新建备份任务' }}</h3>

        <label>从模板开始（可选）</label>
        <select :value="form.template_key" @change="applyTemplate($event.target.value)">
          <option value="">— 自定义 —</option>
          <optgroup v-for="(list, group) in groupedTemplates" :key="group" :label="group">
            <option v-for="t in list" :key="t.key" :value="t.key">{{ t.label }}</option>
          </optgroup>
        </select>
        <p v-if="form.template_key" class="small muted" style="margin:6px 0 0">
          {{ templates.find((x) => x.key === form.template_key)?.notes }}
        </p>

        <div class="grid2" style="margin-top:14px">
          <div><label>任务名称 *</label><input v-model="form.name" /></div>
          <div>
            <label>数据库实例</label>
            <select v-model="form.db_instance_id">
              <option :value="null">不关联</option>
              <option v-for="d in instances" :key="d.id" :value="d.id">
                {{ d.name }}（{{ d.db_type }}）
              </option>
            </select>
          </div>
          <div>
            <label>执行服务器 *<span class="muted">（在其上运行备份命令）</span></label>
            <select v-model="form.exec_server_id">
              <option :value="null">请选择</option>
              <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.host }})</option>
            </select>
          </div>
          <div>
            <label>备份类型</label>
            <select v-model="form.backup_type">
              <option value="logical">逻辑备份（导出）</option>
              <option value="physical">物理 / 文件级备份</option>
              <option value="config">配置备份</option>
            </select>
          </div>
          <div><label>备份目录</label><input v-model="form.target_dir" class="mono" /></div>
          <div><label>保留天数</label><input v-model.number="form.retention_days" type="number" /></div>
          <div>
            <label>调度周期（cron）</label>
            <input v-model="form.schedule_cron" class="mono" placeholder="0 2 * * *" />
          </div>
          <div><label>超时（秒）</label><input v-model.number="form.timeout_sec" type="number" /></div>
        </div>

        <label style="margin-top:14px">备份命令 *</label>
        <textarea v-model="form.script" rows="12" spellcheck="false" class="mono"></textarea>
        <p class="small muted" style="margin:6px 0 0">
          可用占位符：
          <code v-for="p in placeholders" :key="p" class="mono">{{ p }}</code>
          —— 密码请在命令中使用 <code class="mono">$MYSQL_PWD</code> /
          <code class="mono">$PGPASSWORD</code>，平台会自动注入，不会出现在命令行里。
        </p>

        <label class="row" style="gap:6px;margin-top:14px">
          <input type="checkbox" v-model="form.enabled" style="width:auto" />
          <span style="color:var(--text)">创建后启用定时调度</span>
        </label>

        <div class="row" style="margin-top:20px;justify-content:flex-end">
          <button @click="showForm = false">取消</button>
          <button class="primary" @click="save">保存</button>
        </div>
      </div>
    </div>

    <!-- 执行历史 -->
    <div v-if="history" class="modal-mask" @click.self="history = null">
      <div class="modal" style="width:880px;max-width:94vw">
        <h3>执行历史：{{ history.task.name }}</h3>
        <div v-if="!history.runs.length" class="empty">还没有执行记录</div>
        <table v-else>
          <thead>
            <tr><th>开始时间</th><th>结果</th><th>耗时</th><th>备份文件</th><th>清理</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in history.runs" :key="r.id">
              <td class="small mono muted">{{ new Date(r.started_at).toLocaleString() }}</td>
              <td>
                <span :class="['badge', r.status === 'success' ? 'ok' : 'offline']">
                  {{ r.status === 'success' ? '成功' : '失败' }}
                </span>
                <span class="small muted mono" style="margin-left:6px">exit={{ r.exit_code }}</span>
              </td>
              <td class="small">{{ r.duration_ms }}ms</td>
              <td class="small mono" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">
                {{ r.artifact || '-' }}
              </td>
              <td class="small">{{ r.purged ? `清理 ${r.purged} 个` : '-' }}</td>
            </tr>
          </tbody>
        </table>

        <details v-if="history.runs.length" style="margin-top:14px">
          <summary class="small muted" style="cursor:pointer">查看最近一次的输出</summary>
          <pre class="raw">{{ history.runs[0].output || '（无输出）' }}
{{ history.runs[0].error ? '\n错误：' + history.runs[0].error : '' }}</pre>
        </details>

        <div class="row" style="margin-top:16px;justify-content:flex-end">
          <button @click="history = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.task { padding: 14px 16px; }
code { background: var(--bg-soft); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.err-text { color: #f87171; }
.raw {
  max-height: 260px; overflow: auto; font-size: 11px; background: #0b0f14;
  padding: 10px; border-radius: 6px; margin: 8px 0 0; white-space: pre-wrap;
  word-break: break-all;
}
</style>
