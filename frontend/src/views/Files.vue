<script setup>
import { computed, onMounted, ref } from 'vue'
import api, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const servers = ref([])
const currentServer = ref(null)
const path = ref('.')
const entries = ref([])
const loading = ref(false)
const uploadTargets = ref([])
const uploadPath = ref('.')
const queue = ref([])
const dragging = ref(false)

async function loadServers() {
  try {
    servers.value = (await api.get('/servers')).data
    if (!currentServer.value && servers.value.length) {
      currentServer.value = servers.value[0].id
      await browse()
    }
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

async function browse(target) {
  if (target) path.value = target
  if (!currentServer.value) return
  loading.value = true
  try {
    const { data } = await api.get('/files/list', {
      params: { server_id: currentServer.value, path: path.value }
    })
    entries.value = data.entries
    path.value = data.path
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
    entries.value = []
  } finally {
    loading.value = false
  }
}

function goUp() {
  const parts = path.value.replace(/\/+$/, '').split('/')
  parts.pop()
  browse(parts.join('/') || '/')
}

function humanSize(bytes) {
  if (!bytes) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`
}

function onDrop(event) {
  dragging.value = false
  const files = Array.from(event.dataTransfer?.files || [])
  if (files.length) enqueue(files)
}

function onPick(event) {
  const files = Array.from(event.target.files || [])
  if (files.length) enqueue(files)
  event.target.value = ''
}

function enqueue(files) {
  if (!uploadTargets.value.length) {
    window.__toast?.('请先选择要上传到的服务器', 'warn')
    return
  }
  for (const file of files) {
    queue.value.push({
      id: Math.random().toString(36).slice(2),
      file,
      name: file.name,
      size: file.size,
      status: 'pending',
      result: null
    })
  }
  processQueue()
}

let running = false
async function processQueue() {
  if (running) return
  running = true
  while (true) {
    const item = queue.value.find((x) => x.status === 'pending')
    if (!item) break
    item.status = 'uploading'
    try {
      const fd = new FormData()
      fd.append('server_ids', uploadTargets.value.join(','))
      fd.append('remote_path', uploadPath.value)
      fd.append('file', item.file)
      const { data } = await api.post('/files/upload', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 0
      })
      item.result = data
      item.status = data.succeeded > 0 ? 'done' : 'failed'
      window.__toast?.(
        `${item.name}：成功 ${data.succeeded}/${data.total} 台`,
        data.failed ? 'warn' : 'ok'
      )
    } catch (e) {
      item.status = 'failed'
      item.result = { error: errMsg(e) }
      window.__toast?.(`${item.name} 上传失败：${errMsg(e)}`, 'err')
    }
  }
  running = false
}

const selectedServers = computed(() => servers.value.filter((s) => currentServer.value === s.id))

onMounted(loadServers)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">文件传输</h2>
      <div class="spacer"></div>
      <select v-model="currentServer" style="width:200px" @change="browse('.')">
        <option v-for="s in servers" :key="s.id" :value="s.id">{{ s.name }} ({{ s.host }})</option>
      </select>
      <button @click="browse()">刷新</button>
    </div>

    <div v-if="!auth.canOperate" class="card small muted">
      当前角色为 {{ auth.role }}（只读），无法上传或下载文件。
    </div>

    <div v-if="!servers.length" class="card empty">
      还没有服务器。请先到「服务器管理」添加。
    </div>

    <template v-else>
      <div class="grid">
        <!-- 远端浏览 -->
        <div class="card col" style="gap:10px">
          <div class="row">
            <button class="sm" @click="goUp" title="上级目录">↑</button>
            <input v-model="path" class="mono" style="flex:1" @keyup.enter="browse()" />
            <button class="sm" @click="browse()">转到</button>
          </div>

          <div v-if="loading" class="empty small">读取中…</div>
          <div v-else class="list scroll">
            <div
              v-for="e in entries"
              :key="e.path"
              class="entry"
              @click="e.is_dir ? browse(e.path) : null"
            >
              <span class="ico">{{ e.is_dir ? '📁' : '📄' }}</span>
              <span class="ename">{{ e.name }}</span>
              <span class="muted small mono">{{ e.is_dir ? '' : humanSize(e.size) }}</span>
            </div>
            <div v-if="!entries.length" class="empty small">目录为空</div>
          </div>
        </div>

        <!-- 上传区 -->
        <div class="card col" style="gap:12px">
          <h3 style="font-size:14px;margin:0">上传到多台服务器</h3>

          <div>
            <label>目标服务器（可多选，将并行上传）</label>
            <div class="targets">
              <label v-for="s in servers" :key="s.id" class="target">
                <input
                  type="checkbox"
                  :value="s.id"
                  v-model="uploadTargets"
                  style="width:auto"
                />
                <span class="small">{{ s.name }}</span>
              </label>
            </div>
          </div>

          <div>
            <label>远端目标目录</label>
            <input v-model="uploadPath" class="mono" />
          </div>

          <div
            :class="['drop', { over: dragging }]"
            @dragover.prevent="dragging = true"
            @dragleave="dragging = false"
            @drop.prevent="onDrop"
            @click="$refs.fileInput.click()"
          >
            <p style="margin:0">拖拽文件到此处，或点击选择</p>
            <p class="small muted" style="margin:6px 0 0">
              将并行上传到上方勾选的所有服务器
            </p>
            <input
              ref="fileInput"
              type="file"
              multiple
              style="display:none"
              @change="onPick"
            />
          </div>

          <div v-if="queue.length" class="queue">
            <div v-for="item in queue" :key="item.id" class="qitem">
              <span class="ename small">{{ item.name }}</span>
              <span class="muted small mono">{{ humanSize(item.size) }}</span>
              <div class="spacer"></div>
              <span
                :class="['badge', item.status === 'done' ? 'ok' : item.status === 'failed' ? 'offline' : 'gray']"
              >
                {{ item.status === 'pending' ? '等待' : item.status === 'uploading' ? '上传中' : item.status === 'done' ? '完成' : '失败' }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.list { max-height: 460px; border: 1px solid var(--border); border-radius: 6px; }
.entry {
  display: flex; align-items: center; gap: 9px; padding: 7px 11px;
  cursor: pointer; border-bottom: 1px solid #1c2430;
}
.entry:hover { background: #1e2733; }
.ico { font-size: 13px; }
.ename { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.targets { display: flex; flex-wrap: wrap; gap: 10px; }
.target { display: flex; align-items: center; gap: 5px; margin: 0; cursor: pointer; }
.drop {
  border: 2px dashed var(--border); border-radius: 8px; padding: 26px;
  text-align: center; cursor: pointer; transition: all .15s;
}
.drop:hover, .drop.over { border-color: var(--accent); background: #16243a; }
.queue { border-top: 1px solid var(--border); padding-top: 10px; }
.qitem { display: flex; align-items: center; gap: 10px; padding: 5px 0; }
</style>
