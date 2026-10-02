import { agentState } from './time.js'
export const CURRENT_DIRECTION = 'VPS_TO_TARGET'
export function targetConfigured(target) { return Boolean(target?.address) && !target.needsConfiguration && ['ICMP', 'TCP'].includes(target.protocol) && (target.protocol !== 'TCP' || Number.isInteger(target.port) && target.port >= 1 && target.port <= 65535) }
export function canMeasureHost(host, targets, now = Date.now()) {
 return Boolean(host?.enabled && agentState(host, now) === 'ONLINE' && !host.agentUpdateRequired && Number.isInteger(host.agentVersion) && host.agentVersion >= 2 && targets.some(target => host.nodeIds?.includes(target.id) && target.enabled && targetConfigured(target)))
}
export function formatEndpoint(address, protocol = 'ICMP', port = null) {
 if (!address) return '未记录地址'
 return protocol === 'TCP' && port != null ? (address.includes(':') ? `[${address}]` : address) + ':' + port : address
}
export function latestMeasurement(results, hostId, target) {
 return results.filter(result => result.hostId === hostId && result.nodeId === target.id && result.direction === CURRENT_DIRECTION && result.protocol === target.protocol && result.targetAddress === target.address && (result.targetPort ?? null) === (target.port ?? null)).sort((a, b) => Date.parse(b.checkedAt) - Date.parse(a.checkedAt))[0]
}
export const measurementProtocol = result => result?.protocol || (result?.direction === 'NODE_TO_VPS' ? 'ICMP' : '')
export const failureLabel = result => measurementProtocol(result) === 'TCP' ? '连接失败率' : '丢包率'
export function failureRate(result) {
 if (!result || result.status === 'ERROR' || !Number.isInteger(result.sent) || !Number.isInteger(result.received) || result.sent <= 0 || result.received < 0 || result.received > result.sent) return '—'
 return Math.round((result.sent - result.received) / result.sent * 100) + '%'
}
export const measurementRtt = result => result?.status !== 'ERROR' && Number.isFinite(result?.avgRttMs) && result.avgRttMs >= 0 ? result.avgRttMs : null
export function measurementLabel(result) {
 if (result?.status === 'ERROR') return '测量失败'
 if (result?.status === 'OK') return measurementProtocol(result) === 'TCP' ? '连接成功' : '收到回复'
 if (result?.status === 'TIMEOUT') return measurementProtocol(result) === 'TCP' ? '连接失败' : 'ICMP 无回复'
 return '未知测量状态'
}
export const directionLabel = result => result.direction === CURRENT_DIRECTION ? 'VPS → 测速目标' : result.direction === 'NODE_TO_VPS' ? '旧版：测试节点 → VPS' : '未记录方向'
