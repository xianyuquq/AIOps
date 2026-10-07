import { defineStore } from 'pinia'
import api, { getToken, setToken } from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null,
    role: '',
    username: '',
    ready: false
  }),
  getters: {
    isLoggedIn: () => !!getToken(),
    /** admin 可管理一切；ops 可执行命令与上传；viewer 只读 */
    canOperate: (s) => ['admin', 'ops'].includes(s.role),
    isAdmin: (s) => s.role === 'admin'
  },
  actions: {
    async login(username, password) {
      const { data } = await api.post('/auth/login/json', { username, password })
      setToken(data.access_token)
      this.username = data.username
      this.role = data.role
      await this.fetchMe()
      return data
    },
    async fetchMe() {
      if (!getToken()) {
        this.ready = true
        return null
      }
      try {
        const { data } = await api.get('/auth/me')
        this.user = data
        this.username = data.username
        this.role = data.role
        return data
      } catch {
        setToken('')
        this.user = null
        this.role = ''
        return null
      } finally {
        this.ready = true
      }
    },
    logout() {
      setToken('')
      this.user = null
      this.role = ''
      this.username = ''
    }
  }
})
