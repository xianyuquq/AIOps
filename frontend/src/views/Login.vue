<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { errMsg } from '../api'

const auth = useAuthStore()
const router = useRouter()
const username = ref('admin')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function submit() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    router.push('/dashboard')
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="submit">
      <h2>AI 智能运维平台</h2>
      <p class="muted small">智能监控 · 批量终端 · AI 根因分析</p>

      <div style="margin-top:22px">
        <label>用户名</label>
        <input v-model="username" autocomplete="username" />
      </div>
      <div style="margin-top:14px">
        <label>密码</label>
        <input v-model="password" type="password" autocomplete="current-password" />
      </div>

      <p v-if="error" class="err">{{ error }}</p>

      <button class="primary" type="submit" :disabled="loading" style="width:100%;margin-top:20px">
        {{ loading ? '登录中…' : '登 录' }}
      </button>

      <p class="hint muted small">
        首次使用默认账号 <code>admin</code> / <code>admin123</code>，登录后请立即修改密码。
      </p>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  height: 100%; display: flex; align-items: center; justify-content: center;
  background: radial-gradient(circle at 50% 30%, #1b2838 0%, #0f1419 70%);
}
.login-card {
  width: 360px; background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 32px;
}
.login-card h2 { margin: 0 0 4px; font-size: 20px; }
.err { color: #f87171; font-size: 13px; margin: 12px 0 0; }
.hint { margin-top: 18px; text-align: center; line-height: 1.6; }
code { background: var(--bg-soft); padding: 1px 5px; border-radius: 4px; }
</style>
