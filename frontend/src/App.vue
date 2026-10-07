<script setup>
import { onMounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const toasts = ref([])

// 全局轻量提示
window.__toast = (message, kind = '') => {
  const id = Date.now() + Math.random()
  toasts.value.push({ id, message, kind })
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }, 3600)
}

const nav = [
  { path: '/dashboard', label: '总览大盘' },
  { path: '/terminal', label: '多终端' },
  { path: '/servers', label: '服务器管理' },
  { path: '/metrics', label: '监控图表' },
  { path: '/scripts', label: '监控脚本' },
  { path: '/alerts', label: '告警中心' },
  { path: '/ai', label: 'AI 助手' },
  { path: '/databases', label: '数据库监控' },
  { path: '/backups', label: '备份与容灾' },
  { path: '/files', label: '文件传输' },
  { path: '/audit', label: '审计日志' }
]

function logout() {
  auth.logout()
  router.push('/login')
}

onMounted(() => auth.fetchMe())
</script>

<template>
  <div v-if="route.meta.public" class="full">
    <router-view />
    <div class="toast-wrap">
      <div v-for="t in toasts" :key="t.id" :class="['toast', t.kind]">{{ t.message }}</div>
    </div>
  </div>

  <div v-else class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="dot"></span>
        <span>AI 智能运维</span>
      </div>
      <nav>
        <router-link v-for="item in nav" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </router-link>
      </nav>
      <div class="user-box">
        <div class="small muted">当前用户</div>
        <div class="row">
          <strong>{{ auth.username || '-' }}</strong>
          <span class="badge gray">{{ auth.role }}</span>
        </div>
        <button class="sm" style="margin-top:8px;width:100%" @click="logout">退出登录</button>
      </div>
    </aside>

    <main class="main scroll">
      <router-view />
    </main>

    <div class="toast-wrap">
      <div v-for="t in toasts" :key="t.id" :class="['toast', t.kind]">{{ t.message }}</div>
    </div>
  </div>
</template>

<style scoped>
.full { height: 100%; }
.layout { display: flex; height: 100%; }

.sidebar {
  width: 190px; flex-shrink: 0; background: var(--bg-soft);
  border-right: 1px solid var(--border); display: flex; flex-direction: column;
  padding: 14px 10px;
}
.brand {
  display: flex; align-items: center; gap: 8px; font-weight: 600;
  padding: 6px 8px 16px; font-size: 15px;
}
.dot {
  width: 9px; height: 9px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 9px var(--accent);
}
nav { display: flex; flex-direction: column; gap: 2px; flex: 1; }
.nav-item {
  padding: 8px 12px; border-radius: 6px; color: var(--text-dim);
  text-decoration: none; font-size: 13px; transition: all .15s;
}
.nav-item:hover { background: #223047; color: var(--text); }
.nav-item.router-link-active { background: var(--accent); color: #fff; font-weight: 500; }
.user-box { border-top: 1px solid var(--border); padding-top: 12px; }

.main { flex: 1; padding: 20px 24px; }
</style>
