import { request } from './client.js'
const route = kind => kind === 'vps' ? '/vps' : '/probes'
export function validateFleet(input, kind) {
 const errors = {}, name = typeof input.name === 'string' ? input.name.trim() : '', region = typeof input.region === 'string' ? input.region.trim() : ''
 if (!name || name.length > 100) errors.name = '名称须为 1–100 个字符'
 if (region.length > 100) errors.region = '地区不能超过 100 个字符'
 if (typeof input.enabled !== 'boolean') errors.enabled = '启用状态无效'
 const address = typeof input.address === 'string' ? input.address.trim() : ''
 if (kind === 'vps') {
  let valid = Boolean(address) && address.length <= 253 && !/[\s/@?#]/.test(address)
  if (address.includes(':')) { try { new URL(`http://[${address}]/`) } catch { valid = false } }
  else if (/^[\d.]+$/.test(address)) valid = valid && address.split('.').length === 4 && address.split('.').every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255)
  else valid = valid && address.split('.').every(part => /^[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?$/i.test(part))
  if (!valid) errors.address = '请输入有效的 IP 或主机名，不含协议、端口或路径'
 }
 const nodeIds = [...new Set(Array.isArray(input.nodeIds) ? input.nodeIds : [])]
 if (kind === 'vps' && input.nodeIds !== undefined && !Array.isArray(input.nodeIds)) errors.nodeIds = '请选择有效的测试节点'
 if (kind === 'vps' && (nodeIds.length > 100 || !nodeIds.every(id => Number.isSafeInteger(id) && id > 0))) errors.nodeIds = '请选择有效的测试节点（最多 100 个）'
 return { errors, value: { name, region, enabled: input.enabled, ...(kind === 'vps' ? { address, nodeIds } : {}) } }
}
function validated(input, kind) { const result = validateFleet(input, kind); if (Object.keys(result.errors).length) throw new Error(Object.values(result.errors)[0]); return result.value }
export const fleetApi = {
 list: () => request('/fleet'),
 create: (kind, input) => request(route(kind), { method: 'POST', body: validated(input, kind) }),
 update: (kind, id, input) => request(`${route(kind)}/${id}`, { method: 'PUT', body: validated(input, kind) }),
 toggle: (kind, id, enabled) => request(`${route(kind)}/${id}/enabled`, { method: 'PATCH', body: { enabled } }),
 remove: (kind, id) => request(`${route(kind)}/${id}`, { method: 'DELETE' }),
 installation: (kind, id) => request(`${route(kind)}/${id}/enrollment`, { method: 'POST', body: {} }),
 check: id => request(`/vps/${id}/checks`, { method: 'POST', body: {} }),
 history: (id, page = 1, size = 8) => request(`/vps/${id}/checks?page=${page}&size=${size}`)
}
