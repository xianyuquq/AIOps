<script setup>
import { onMounted, ref } from 'vue'
import api, { errMsg } from '../api'

const records = ref([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    records.value = (await api.get('/audit/commands', { params: { limit: 300 } })).data.records
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">审计日志</h2>
      <div class="spacer"></div>
      <button @click="load">刷新</button>
    </div>

    <div class="card small muted">
      记录通过平台执行的每条命令：谁、在何时、对哪台服务器执行了什么。
      审计记录只读，不可通过界面修改或删除。
    </div>

    <div class="card">
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!records.length" class="empty">
        还没有审计记录。在「多终端」页面执行命令后会出现在这里。
      </div>
      <table v-else>
        <thead>
          <tr><th>时间</th><th>服务器</th><th>命令</th><th>操作人</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in records" :key="r.id">
            <td class="small muted mono">{{ new Date(r.started_at).toLocaleString() }}</td>
            <td>{{ r.server_name || `#${r.server_id}` }}</td>
            <td class="mono small">{{ r.command }}</td>
            <td class="small muted">{{ r.operator ?? '-' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
