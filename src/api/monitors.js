import { request } from './client.js'
import { requireMonitor } from './model.js'
export const monitorsApi = {
 list: () => request('/monitors'),
 detail: id => request(`/monitors/${id}`),
 create: input => request('/monitors', { method: 'POST', body: requireMonitor(input) }),
 update: (id, input) => request(`/monitors/${id}`, { method: 'PUT', body: requireMonitor(input) }),
 remove: id => request(`/monitors/${id}`, { method: 'DELETE' }),
 setEnabled: (id, enabled) => request(`/monitors/${id}/enabled`, { method: 'PATCH', body: { enabled } }),
 check: (id, timeoutMs = 5000) => request(`/monitors/${id}/check`, { method: 'POST', body: {}, timeout: Math.min(Number(timeoutMs) + 10000, 45000) }),
 history: (id, page = 1, size = 8) => request(`/monitors/${id}/checks?page=${page}&size=${size}`)
}
