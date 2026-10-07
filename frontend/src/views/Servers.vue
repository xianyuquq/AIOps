<script setup>
import { onMounted, reactive, ref } from 'vue'
import api, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const servers = ref([])
const groups = ref([])
const loading = ref(true)
const showForm = ref(false)
const editing = ref(null)
const testing = ref({})
const diagnosing = ref({})
const diagnosis = ref(null)

const blank = () => ({
  name: '', host: '', port: 22, username: 'root', protocol: 'ssh',
  auth_type: 'password', secret: '', group_id: null, tags: {}
})
const form = reactive(blank())

async function load() {
  loading.value = true
  try {
    const [s, g] = await Promise.all([api.get('/servers'), api.get('/servers/groups')])
    servers.value = s.data
    groups.value = g.data
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, blank())
  editing.value = null
  showForm.value = true
}

function openEdit(s) {
  Object.assign(form, {
    name: s.name, host: s.host, port: s.port, username: s.username,
    protocol: s.protocol, auth_type: s.auth_type, secret: '',
    group_id: s.group_id, tags: s.tags || {}
  })
  editing.value = s
  showForm.value = true
}

async function save() {
  if (!form.name || !form.host || !form.username) {
    window.__toast?.('名称、主机、用户名必填', 'warn')
    return
  }
  try {
    const payload = { ...form, port: Number(form.port), group_id: form.group_id || null }
    if (!payload.secret) delete payload.secret
    if (editing.value) await api.patch(`/servers/${editing.value.id}`, payload)
    else await api.post('/servers', payload)
    window.__toast?.(editing.value ? '已更新' : '已添加', 'ok')
    showForm.value = false
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function test(s) {
  testing.value = { ...testing.value, [s.id]: true }
  try {
    const { data } = await api.post(`/servers/${s.id}/test`)
    window.__toast?.(
      `${s.name}：${data.ok ? '连接成功' : '连接失败'} ${data.ok ? '' : data.message}`,
      data.ok ? 'ok' : 'err'
    )
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    testing.value = { ...testing.value, [s.id]: false }
  }
}

/** 分阶段诊断：能明确指出是网络、算法、认证还是会话阶段的问题。 */
async function diagnose(s) {
  diagnosing.value = { ...diagnosing.value, [s.id]: true }
  try {
    const { data } = await api.post(`/servers/${s.id}/diagnose`)
    diagnosis.value = {
      name: s.name,
      target: data.target,
      ok: data.ok,
      text: data.steps.map((x) => `${x.ok ? '✓' : '✕'} ${x.step}\n    ${x.detail}`).join('\n')
    }
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    diagnosing.value = { ...diagnosing.value, [s.id]: false }
  }
}

async function remove(s) {
  if (!confirm(`确定删除服务器「${s.name}」？`)) return
  try {
    await api.delete(`/servers/${s.id}`)
    window.__toast?.('已删除', 'ok')
    await load()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function createGroup() {
  const name = prompt('新分组名称：')
  if (!name) return
  try {
    await api.post('/servers/groups', { name, description: '' })
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
      <h2 style="margin:0;font-size:19px">服务器管理</h2>
      <div class="spacer"></div>
      <button @click="createGroup" :disabled="!auth.canOperate">新建分组</button>
      <button class="primary" @click="openCreate" :disabled="!auth.canOperate">添加服务器</button>
    </div>

    <div v-if="!auth.canOperate" class="card small muted">
      当前角色为 {{ auth.role }}（只读），无法添加或修改服务器。
    </div>

    <div class="card">
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!servers.length" class="empty">
        还没有服务器。点击右上角「添加服务器」开始。
      </div>
      <table v-else>
        <thead>
          <tr>
            <th>名称</th><th>地址</th><th>认证</th><th>凭据</th><th>分组</th>
            <th>状态</th><th>最后在线</th><th style="width:200px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in servers" :key="s.id">
            <td><strong>{{ s.name }}</strong></td>
            <td class="mono small">{{ s.username }}@{{ s.host }}:{{ s.port }}</td>
            <td class="small">{{ s.auth_type === 'password' ? '密码' : '私钥' }}</td>
            <td>
              <!-- 明确显示凭据是否已保存：这是「连不上」最常见的隐藏原因 -->
              <span v-if="s.has_secret" class="badge ok">已保存</span>
              <span v-else class="badge offline" title="没有保存密码或私钥，必然无法认证">未保存</span>
            </td>
            <td class="small muted">
              {{ groups.find((g) => g.id === s.group_id)?.name || '-' }}
            </td>
            <td>
              <span :class="['badge', s.status === 'online' ? 'ok' : s.status === 'offline' ? 'offline' : 'unknown']">
                {{ s.status }}
              </span>
            </td>
            <td class="small muted mono">
              {{ s.last_seen ? new Date(s.last_seen).toLocaleString() : '-' }}
            </td>
            <td>
              <button class="sm" :disabled="testing[s.id] || !auth.canOperate" @click="test(s)">
                {{ testing[s.id] ? '测试中' : '测试' }}
              </button>
              <button class="sm" style="margin-left:4px" :disabled="diagnosing[s.id]" @click="diagnose(s)">
                {{ diagnosing[s.id] ? '诊断中' : '诊断' }}
              </button>
              <button class="sm" style="margin-left:4px" :disabled="!auth.canOperate" @click="openEdit(s)">编辑</button>
              <button class="sm danger" style="margin-left:4px" :disabled="!auth.canOperate" @click="remove(s)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="servers.some((s) => s.last_error)" class="card">
      <h3 style="font-size:14px;margin:0 0 10px">最近的连接错误</h3>
      <div v-for="s in servers.filter((x) => x.last_error)" :key="s.id" class="err-row">
        <span class="badge offline">{{ s.name }}</span>
        <span class="small mono">{{ s.last_error }}</span>
      </div>
    </div>

    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">      <div class="modal">
        <h3>{{ editing ? '编辑服务器' : '添加服务器' }}</h3>
        <div class="form-grid">
          <div><label>名称 *</label><input v-model="form.name" placeholder="web-01" /></div>
          <div><label>主机地址 *</label><input v-model="form.host" placeholder="192.168.1.10" /></div>
          <div><label>端口</label><input v-model.number="form.port" type="number" /></div>
          <div><label>用户名 *</label><input v-model="form.username" /></div>
          <div>
            <label>协议</label>
            <select v-model="form.protocol">
              <option value="ssh">SSH（Linux / 麒麟 / 统信）</option>
              <option value="winrm">WinRM（Windows Server）</option>
            </select>
          </div>
          <div>
            <label>认证方式</label>
            <select v-model="form.auth_type">
              <option value="password">密码</option>
              <option value="private_key">私钥</option>
            </select>
          </div>
          <div style="grid-column:1/-1">
            <label>
              {{ form.auth_type === 'password' ? '密码' : '私钥内容（PEM）' }}
              {{ editing ? '（留空表示不修改）' : '' }}
            </label>
            <textarea
              v-model="form.secret"
              :rows="form.auth_type === 'private_key' ? 5 : 1"
              :placeholder="editing ? '留空则保持原凭据不变' : ''"
            ></textarea>
            <p class="small muted" style="margin:6px 0 0">
              凭据使用 AES-256-GCM 加密后存储，不会以明文出现在任何接口响应中。
            </p>
          </div>
          <div>
            <label>分组</label>
            <select v-model="form.group_id">
              <option :value="null">未分组</option>
              <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
            </select>
          </div>
        </div>

        <div class="row" style="margin-top:20px;justify-content:flex-end">
          <button @click="showForm = false">取消</button>
          <button class="primary" @click="save">保存</button>
        </div>
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
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.err-row { display: flex; gap: 10px; align-items: center; padding: 5px 0; }
.diag-summary { padding: 10px 14px; border-radius: 7px; font-size: 13px; margin-bottom: 12px; }
.diag-summary.ok { background: #14351f; color: #4ade80; }
.diag-summary.bad { background: #3a1a1a; color: #fca5a5; }
.diag-text {
  background: #0b0f14; padding: 14px; border-radius: 7px; font-size: 12px;
  line-height: 1.8; white-space: pre-wrap; word-break: break-word;
  max-height: 380px; overflow: auto; margin: 0; color: var(--text);
}
</style>
