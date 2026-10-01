import axios from 'axios'
import { demoApi } from './demo.js'
const client = axios.create({ baseURL: import.meta.env.VITE_API_BASE_URL || '/api', timeout: 10000 })
client.interceptors.response.use(response => { if (response.data?.code !== 0) throw new Error(response.data?.message || '服务器返回了无法识别的响应'); return response.data.data }, error => Promise.reject(new Error(error.response?.data?.message || (error.code === 'ECONNABORTED' ? '请求超时，请稍后重试' : error.message || '无法连接服务器'))))
export const isDemo = import.meta.env.VITE_DATA_MODE !== 'api'
const realApi = {
 list: () => client.get('/monitors'), detail: id => client.get(`/monitors/${id}`), create: data => client.post('/monitors', data), update: (id, data) => client.put(`/monitors/${id}`, data), remove: id => client.delete(`/monitors/${id}`), setEnabled: (id, enabled) => client.patch(`/monitors/${id}/enabled`, { enabled }), check: (id, timeoutMs = 5000) => client.post(`/monitors/${id}/check`, null, { timeout: Math.min(Number(timeoutMs) + 5000, 2147483647) }), history: (id, page = 1, size = 8) => client.get(`/monitors/${id}/checks`, { params: { page, size } }),
}
export const monitorsApi = isDemo ? demoApi : realApi
