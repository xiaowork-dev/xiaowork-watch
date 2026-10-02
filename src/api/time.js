export const HEARTBEAT_MAX_AGE_MS = 90000
export function agentState(item, now) {
 if (!item || item.agentState === 'PENDING') return 'PENDING'
 const at = Date.parse(item.lastSeenAt)
 if (!Number.isFinite(at) || now - at > HEARTBEAT_MAX_AGE_MS) return 'OFFLINE'
 return item.agentState === 'ONLINE' ? 'ONLINE' : 'OFFLINE'
}
export function resultIsStale(result, now) { const at = Date.parse(result?.checkedAt); return !Number.isFinite(at) || now - at > 180000 }
export function relativeTime(value, now) {
 const at = Date.parse(value)
 if (!Number.isFinite(at)) return '—'
 const seconds = Math.max(0, Math.floor((now - at) / 1000))
 return seconds < 60 ? '刚刚' : seconds < 3600 ? Math.floor(seconds / 60) + ' 分钟前' : seconds < 86400 ? Math.floor(seconds / 3600) + ' 小时前' : Math.floor(seconds / 86400) + ' 天前'
}
