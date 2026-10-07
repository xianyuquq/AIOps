import axios from 'axios'

const api = axios.create({ baseURL: '/api/v1', timeout: 60000 })

export const TOKEN_KEY = 'aiops_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      setToken('')
      if (!location.hash.includes('/login')) location.hash = '#/login'
    }
    return Promise.reject(error)
  }
)

/** 把后端错误转换为可展示的中文消息。 */
export function errMsg(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
  return error?.message || '请求失败'
}

export default api
