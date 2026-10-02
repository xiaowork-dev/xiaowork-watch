import { request } from './client.js'
const route = kind => kind === 'vps' ? '/vps' : '/probes'
function invalid(message, field = 'address') { const error = new Error(message); error.field = field; throw error }
export function normalizeAddress(input) {
 let address = typeof input === 'string' ? input.trim() : ''
 if (address.startsWith('[') && address.endsWith(']')) {
  address = address.slice(1, -1)
  if (!address.includes(':')) invalid('方括号仅用于 IPv6 地址')
 }
 if (!address || address.length > 253 || /[\s/@?#\\%]/.test(address)) invalid('请输入 IP 或主机名，不含协议、路径或特殊字符')
 if (address.includes(':')) {
  try { address = new URL(`http://[${address}]/`).hostname.slice(1, -1) } catch { invalid('IPv6 地址无效；带端口时使用 [IPv6]:端口') }
 } else if (/^[\d.]+$/.test(address)) {
  if (address.split('.').length !== 4 || !address.split('.').every(part => /^(0|[1-9]\d{0,2})$/.test(part) && Number(part) <= 255)) invalid('请输入有效 IPv4 地址')
 } else {
  if (!address.split('.').every(part => /^[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?$/i.test(part))) invalid('请输入有效主机名')
  address = address.toLowerCase()
 }
 return address
}
export function parseTarget(input, protocol, explicitPort = null) {
 if (!['ICMP', 'TCP'].includes(protocol)) invalid('请选择 ICMP 或 TCP', 'protocol')
 const text = typeof input === 'string' ? input.trim() : ''
 let address = text, embeddedPort = null
 const bracketed = text.match(/^\[([^\]]+)\](?::([^:]+))?$/)
 if (bracketed) {
  address = bracketed[1]; embeddedPort = bracketed[2] ?? null
  if (!address.includes(':')) invalid('方括号仅用于 IPv6 地址')
 }
 else if ((text.match(/:/g) || []).length === 1) { const pieces = text.split(':'); address = pieces[0]; embeddedPort = pieces[1] }
 const supplied = explicitPort !== null && explicitPort !== undefined && explicitPort !== ''
 if (protocol === 'ICMP' && (embeddedPort !== null || supplied)) invalid('ICMP 不使用端口；带端口的目标请选择 TCP', 'port')
 address = normalizeAddress(address)
 if (protocol === 'ICMP') return { address, protocol, port: null }
 const parsePort = value => {
  if (!/^[0-9]{1,5}$/.test(String(value)) || !Number.isSafeInteger(Number(value)) || Number(value) < 1 || Number(value) > 65535) invalid('TCP 端口须为 1–65535 的整数', 'port')
  return Number(value)
 }
 const fromAddress = embeddedPort !== null ? parsePort(embeddedPort) : null
 const fromField = supplied ? parsePort(explicitPort) : null
 if (fromAddress !== null && fromField !== null && fromAddress !== fromField) invalid('地址中的端口与端口字段不一致', 'port')
 const port = fromAddress ?? fromField
 if (port === null) invalid('请填写 TCP 端口；也可输入主机名:端口或 [IPv6]:端口', 'port')
 return { address, protocol, port }
}
export function validateFleet(input, kind) {
 const errors = {}, name = typeof input.name === 'string' ? input.name.trim() : '', region = typeof input.region === 'string' ? input.region.trim() : ''
 if (!name || name.length > 100) errors.name = '名称须为 1–100 个字符'
 if (region.length > 100) errors.region = '地区不能超过 100 个字符'
 if (typeof input.enabled !== 'boolean') errors.enabled = '启用状态无效'
 let address = '', target = {}
 if (kind === 'vps') {
  try { address = normalizeAddress(input.address) } catch (error) { errors.address = error.message }
 } else { try { target = parseTarget(input.address, input.protocol, input.port) } catch (error) { errors[error.field || 'address'] = error.message } }
 const nodeIds = [...new Set(Array.isArray(input.nodeIds) ? input.nodeIds : [])]
 if (kind === 'vps' && input.nodeIds !== undefined && !Array.isArray(input.nodeIds)) errors.nodeIds = '请选择有效的测速目标'
 if (kind === 'vps' && (nodeIds.length > 100 || !nodeIds.every(id => Number.isSafeInteger(id) && id > 0))) errors.nodeIds = '请选择有效的测速目标（最多 100 个）'
 return { errors, value: { name, region, enabled: input.enabled, ...(kind === 'vps' ? { address, nodeIds } : target) } }
}
function validated(input, kind) { const result = validateFleet(input, kind); if (Object.keys(result.errors).length) throw new Error(Object.values(result.errors)[0]); return result.value }
export const fleetApi = {
 list: () => request('/fleet'),
 create: (kind, input) => request(route(kind), { method: 'POST', body: validated(input, kind) }),
 update: (kind, id, input) => request(`${route(kind)}/${id}`, { method: 'PUT', body: validated(input, kind) }),
 toggle: (kind, id, enabled) => request(`${route(kind)}/${id}/enabled`, { method: 'PATCH', body: { enabled } }),
 remove: (kind, id) => request(`${route(kind)}/${id}`, { method: 'DELETE' }),
 installation: (kind, id) => kind === 'vps' ? request(`/vps/${id}/enrollment`, { method: 'POST', body: {} }) : Promise.reject(new Error('测速目标无需安装探针')),
 check: id => request(`/vps/${id}/checks`, { method: 'POST', body: {} }),
 history: (id, page = 1, size = 8) => request(`/vps/${id}/checks?page=${page}&size=${size}`)
}
