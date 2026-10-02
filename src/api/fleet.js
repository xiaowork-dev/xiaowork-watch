import axios from 'axios'
import { isDemo } from './monitors.js'

const KEY = 'xiaowork-watch-fleet-v1'
const clone = value => JSON.parse(JSON.stringify(value))
const wait = () => new Promise(resolve => setTimeout(resolve, 120))
const now = () => new Date().toISOString()
export function createFleetSeed(time = Date.now()) {
 const stamp = seconds => new Date(time - seconds * 1000).toISOString()
 const nodes = [
  { id: 1, name: '上海测试节点', region: '中国 · 上海', enabled: true, agentState: 'ONLINE', lastSeenAt: stamp(15) },
  { id: 2, name: '东京测试节点', region: '日本 · 东京', enabled: true, agentState: 'ONLINE', lastSeenAt: stamp(20) },
  { id: 3, name: '法兰克福测试节点', region: '德国 · 法兰克福', enabled: true, agentState: 'OFFLINE', lastSeenAt: stamp(3600) },
 ]
 const hosts = [
  { id: 1, name: '香港 VPS', address: '192.0.2.10', region: '中国 · 香港', enabled: true, nodeIds: [1, 2, 3], agentState: 'ONLINE', lastSeenAt: stamp(12) },
  { id: 2, name: '洛杉矶 VPS', address: '198.51.100.20', region: '美国 · 洛杉矶', enabled: true, nodeIds: [1, 2], agentState: 'OFFLINE', lastSeenAt: stamp(900) },
  { id: 3, name: '新加坡 VPS', address: '203.0.113.30', region: '新加坡', enabled: true, nodeIds: [1, 2], agentState: 'PENDING', lastSeenAt: null },
 ]
 const results = []
 for (const host of hosts.slice(0, 2)) for (const node of nodes.filter(n => host.nodeIds.includes(n.id))) for (let i = 0; i < 8; i++) {
  const fail = host.id === 2 && node.id === 1
  results.push({ id: `seed-${host.id}-${node.id}-${i}`, hostId: host.id, nodeId: node.id, nodeName: node.name, region: node.region, sent: 5, received: fail ? 0 : 5, avgRttMs: fail ? null : host.id === 1 ? (node.id === 1 ? 38 : 64) + i * 2 : 146 + i * 2, status: fail ? 'TIMEOUT' : 'OK', checkedAt: stamp(node.id === 3 ? 3600 + i * 60 : 30 + i * 60) })
 }
 return { version: 1, nextHostId: 4, nextNodeId: 4, hosts, nodes, results }
}
function save(db) { try { localStorage.setItem(KEY, JSON.stringify(db)) } catch { throw new Error('示例数据保存失败，请允许浏览器使用本地存储。') } }
function load() {
 let raw
 try { raw = localStorage.getItem(KEY) } catch { throw new Error('无法读取浏览器中的 VPS 示例数据。') }
 if (!raw) { const db = createFleetSeed(); save(db); return db }
 try { const db = JSON.parse(raw); if (db.version !== 1 || !Array.isArray(db.hosts) || !Array.isArray(db.nodes) || !Array.isArray(db.results) || !Number.isSafeInteger(db.nextHostId) || !Number.isSafeInteger(db.nextNodeId)) throw new Error(); return db }
 catch { throw new Error('VPS 示例数据无法读取，请清除此网站的本地示例数据后重试。') }
}
const items = (db, kind) => kind === 'vps' ? db.hosts : db.nodes
function find(db, kind, id) { const value = items(db, kind).find(x => x.id === Number(id)); if (!value) throw new Error('该目标已不存在，请刷新。'); return value }
function isHostAddress(address) {
 if (!address || address.length > 253 || /[\s/@?#]/.test(address)) return false
 if (/^[\d.]+$/.test(address)) return address.split('.').length === 4 && address.split('.').every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255)
 if (address.includes(':')) { try { return new URL(`http://[${address}]/`).hostname.startsWith('[') } catch { return false } }
 return address.split('.').every(part => /^[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?$/i.test(part))
}
export function validateFleet(input, kind) {
 const errors = {}, name = String(input.name || '').trim(), region = String(input.region || '').trim(), address = String(input.address || '').trim()
 if (!name || name.length > 100) errors.name = '请输入 1–100 个字符的名称'
 if (region.length > 100) errors.region = '地区不能超过 100 个字符'
 if (kind === 'vps' && !isHostAddress(address)) errors.address = '请输入有效 IP 或主机名，不含协议、端口与路径'
 const nodeIds = [...new Set(Array.isArray(input.nodeIds) ? input.nodeIds.map(Number) : [])]
 if (nodeIds.some(id => !Number.isSafeInteger(id) || id < 1)) errors.nodeIds = '测试节点选择无效'
 return { errors, value: { name, region, enabled: Boolean(input.enabled), ...(kind === 'vps' ? { address, nodeIds } : {}) } }
}
const demo = {
 async list() { await wait(); return clone(load()) },
 async create(kind, input) {
  const { errors, value } = validateFleet(input, kind); if (Object.keys(errors).length) throw new Error(Object.values(errors)[0])
  await wait(); const db = load()
  if (kind === 'vps' && value.nodeIds.some(id => !db.nodes.some(n => n.id === id))) throw new Error('测试节点已变更，请重新选择。')
  const item = { ...value, id: kind === 'vps' ? db.nextHostId++ : db.nextNodeId++, agentState: 'PENDING', lastSeenAt: null }
  items(db, kind).push(item); save(db); return clone(item)
 },
 async update(kind, id, input) {
  const { errors, value } = validateFleet(input, kind); if (Object.keys(errors).length) throw new Error(Object.values(errors)[0])
  await wait(); const db = load(), item = find(db, kind, id)
  if (kind === 'vps' && value.nodeIds.some(nodeId => !db.nodes.some(n => n.id === nodeId))) throw new Error('测试节点已变更，请重新选择。')
  if (kind === 'vps' && item.address !== value.address) db.results = db.results.filter(r => r.hostId !== Number(id))
  Object.assign(item, value); save(db); return clone(item)
 },
 async toggle(kind, id, enabled) { if (typeof enabled !== 'boolean') throw new Error('启用状态无效'); await wait(); const db = load(); find(db, kind, id).enabled = enabled; save(db) },
 async remove(kind, id) {
  await wait(); const db = load(); find(db, kind, id)
  if (kind === 'vps') { db.hosts = db.hosts.filter(h => h.id !== Number(id)); db.results = db.results.filter(r => r.hostId !== Number(id)) }
  else { db.nodes = db.nodes.filter(n => n.id !== Number(id)); db.hosts.forEach(h => { h.nodeIds = h.nodeIds.filter(nodeId => nodeId !== Number(id)) }) }
  save(db)
 },
 async installation(kind, id) {
  find(load(), kind, id)
  return { demo: true, command: `# 安装命令模板：当前原型不可执行\n# 主控后端和 Linux 探针发布后生成真实命令\n# curl -fsSL "$CONTROL_URL/agent/install.sh" -o watch-install.sh\n# sudo bash watch-install.sh --server "$CONTROL_URL" --role ${kind === 'vps' ? 'vps' : 'probe'} --enrollment-token "$ONE_TIME_TOKEN"`, expiresAt: null }
 },
 async registerDemo(kind, id) { await wait(); const db = load(), item = find(db, kind, id); item.agentState = 'ONLINE'; item.lastSeenAt = now(); save(db) },
 async check(id) {
  await new Promise(resolve => setTimeout(resolve, 500)); const db = load(), host = find(db, 'vps', id)
  if (!host.enabled) throw new Error('请先启用该 VPS 监控。')
  const nodes = db.nodes.filter(n => host.nodeIds.includes(n.id) && n.enabled && n.agentState === 'ONLINE')
  if (!nodes.length) throw new Error('没有可用的在线测试节点，请先添加并演示注册节点。')
  const stamp = now()
  nodes.forEach(node => {
   const fail = host.id === 2 && node.id === 1
   db.results.push({ id: globalThis.crypto.randomUUID(), hostId: host.id, nodeId: node.id, nodeName: node.name, region: node.region, sent: 5, received: fail ? 0 : 5, avgRttMs: fail ? null : host.id === 1 ? node.id === 1 ? 38 : 64 : 146, status: fail ? 'TIMEOUT' : 'OK', checkedAt: stamp })
  })
  save(db); return { count: nodes.length }
 },
 async history(id, page = 1, size = 8) {
  await wait(); const db = load(); find(db, 'vps', id)
  const all = db.results.filter(r => r.hostId === Number(id)).sort((a, b) => new Date(b.checkedAt) - new Date(a.checkedAt))
  return { records: clone(all.slice((page - 1) * size, page * size)), total: all.length, page, size }
 },
}
const client = axios.create({ baseURL: import.meta.env.VITE_API_BASE_URL || '/api', timeout: 10000 })
client.interceptors.response.use(r => { if (r.data?.code !== 0) throw new Error(r.data?.message || '服务器响应无法识别'); return r.data.data }, e => Promise.reject(new Error(e.response?.data?.message || '无法连接主控后端')))
const route = kind => kind === 'vps' ? '/vps' : '/probes'
const real = {
 list: () => client.get('/fleet'), create: (kind, data) => client.post(route(kind), data), update: (kind, id, data) => client.put(`${route(kind)}/${id}`, data), toggle: (kind, id, enabled) => client.patch(`${route(kind)}/${id}/enabled`, { enabled }), remove: (kind, id) => client.delete(`${route(kind)}/${id}`), installation: (kind, id) => client.post(`${route(kind)}/${id}/enrollment`), check: id => client.post(`/vps/${id}/checks`), history: (id, page = 1, size = 8) => client.get(`/vps/${id}/checks`, { params: { page, size } }),
}
export const fleetApi = isDemo ? demo : real
