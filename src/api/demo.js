import { requireMonitor } from './model.js'
const STORAGE_KEY = 'xiaowork-watch-prototype-v1'
const delay = (ms = 140) => new Promise(resolve => setTimeout(resolve, ms))
const clone = value => JSON.parse(JSON.stringify(value))
export function createSeed(now = Date.now()) {
 const definitions = [
  ['个人博客', 'https://blog.xiaowork.example', 'B', 'purple', 128, true, 'UP'],
  ['公共 API', 'https://api.xiaowork.example/health', 'A', 'blue', 86, true, 'UP'],
  ['导航站', 'https://links.xiaowork.example', 'N', 'orange', 213, true, 'UP'],
  ['文件服务', 'https://files.xiaowork.example', 'F', 'pink', 502, true, 'DOWN'],
  ['开发环境', 'https://dev.xiaowork.example', 'D', 'teal', null, true, 'UNKNOWN'],
  ['备用接口', 'https://backup.xiaowork.example/health', 'R', 'gray', 156, false, 'UP'],
 ]
 const checks = []
 const monitors = definitions.map(([name, url, initial, color, ms, enabled, status], index) => {
  const id = index + 1, lastTime = now - (enabled ? 20000 + index * 6000 : 900000)
  if (status !== 'UNKNOWN') for (let i = 0; i < 24; i++) {
   const failure = id === 4 && i < 5
   checks.push({ id: `${id}-${i}`, monitorId: id, success: !failure, httpCode: failure ? 503 : 200, responseTimeMs: ms + (i === 0 ? 0 : (i % 5 - 2) * 9), errorType: failure ? 'HTTP_STATUS' : null, errorMessage: failure ? '返回 HTTP 503，目标服务暂时不可用。' : null, checkedAt: new Date(lastTime - i * 60000).toISOString() })
  }
  return { id, name, url, initial, color, method: 'GET', intervalSeconds: 60, timeoutMs: 5000, enabled, lastStatus: status, lastHttpCode: status === 'UNKNOWN' ? null : id === 4 ? 503 : 200, lastResponseTimeMs: ms, lastCheckedAt: status === 'UNKNOWN' ? null : new Date(lastTime).toISOString(), createdAt: new Date(now - 86400000).toISOString(), updatedAt: new Date(now - 86400000).toISOString(), demoFailure: id === 4 }
 })
 return { version: 1, nextId: 7, monitors, checks }
}
function load() {
 let stored
 try { stored = localStorage.getItem(STORAGE_KEY) } catch { throw new Error('浏览器无法读取本地示例数据，请允许此网站使用本地存储。') }
 if (!stored) { const seed = createSeed(); save(seed); return seed }
 try { const db = JSON.parse(stored); if (db.version !== 1 || !Array.isArray(db.monitors) || !Array.isArray(db.checks) || !Number.isSafeInteger(db.nextId)) throw new Error(); return db }
 catch { throw new Error('本地示例数据无法读取。请清除此网站的本地数据后重新载入。') }
}
function save(db) { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(db)) } catch { throw new Error('示例数据未保存：浏览器存储不可用或空间不足。') } }
function find(db, id) { const monitor = db.monitors.find(m => m.id === Number(id)); if (!monitor) throw new Error('该监控目标不存在，请刷新列表。'); return monitor }
export const demoApi = {
 async list() { await delay(); return clone(load().monitors) },
 async detail(id) { await delay(); return clone(find(load(), id)) },
 async create(input) {
  const value = requireMonitor(input); await delay(220)
  const db = load(), now = new Date().toISOString()
  const monitor = { ...value, id: db.nextId++, initial: value.name[0].toUpperCase(), color: 'purple', lastStatus: 'UNKNOWN', lastHttpCode: null, lastResponseTimeMs: null, lastCheckedAt: null, createdAt: now, updatedAt: now }
  db.monitors.push(monitor); save(db); return clone(monitor)
 },
 async update(id, input) { const value = requireMonitor(input); await delay(220); const db = load(), monitor = find(db, id); Object.assign(monitor, value, { updatedAt: new Date().toISOString() }); save(db); return clone(monitor) },
 async remove(id) { await delay(); const db = load(); find(db, id); db.monitors = db.monitors.filter(m => m.id !== Number(id)); db.checks = db.checks.filter(c => c.monitorId !== Number(id)); save(db) },
 async setEnabled(id, enabled) {
  if (typeof enabled !== 'boolean') throw new Error('启用状态必须为 true 或 false')
  await delay(); const db = load(), monitor = find(db, id); monitor.enabled = enabled; monitor.updatedAt = new Date().toISOString(); save(db); return clone(monitor)
 },
 async check(id) {
  if (!find(load(), id).enabled) throw new Error('此监控已停用，请先启用。')
  await delay(700)
  const db = load(), monitor = find(db, id)
  if (!monitor.enabled) throw new Error('此监控已停用，演示检测已取消。')
  const failure = Boolean(monitor.demoFailure), checkedAt = new Date().toISOString()
  const result = { id: `check-${Date.now()}-${monitor.id}`, monitorId: monitor.id, success: !failure, httpCode: failure ? 503 : 200, responseTimeMs: failure ? 502 : 128, errorType: failure ? 'HTTP_STATUS' : null, errorMessage: failure ? '返回 HTTP 503，目标服务暂时不可用。' : null, checkedAt }
  db.checks.unshift(result); Object.assign(monitor, { lastStatus: failure ? 'DOWN' : 'UP', lastHttpCode: result.httpCode, lastResponseTimeMs: result.responseTimeMs, lastCheckedAt: checkedAt, updatedAt: checkedAt }); save(db); return clone(result)
 },
 async history(id, page = 1, size = 8) {
  if (!Number.isSafeInteger(page) || page < 1 || !Number.isSafeInteger(size) || size < 1) throw new Error('分页参数必须为正整数')
  await delay(); const db = load(); find(db, id)
  const all = db.checks.filter(c => c.monitorId === Number(id)).sort((a, b) => new Date(b.checkedAt) - new Date(a.checkedAt))
  return { records: clone(all.slice((page - 1) * size, page * size)), total: all.length, page, size }
 }
}
