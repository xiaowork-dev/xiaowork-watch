export function validateMonitor(input) {
 const errors = {}, name = typeof input.name === 'string' ? input.name.trim() : '', url = typeof input.url === 'string' ? input.url.trim() : ''
 if (!name) errors.name = '请输入监控名称'
 else if (name.length > 100) errors.name = '名称不能超过 100 个字符'
 try { if (!['https:', 'http:'].includes(new URL(url).protocol)) throw new Error() } catch { errors.url = '请输入有效的 HTTP 或 HTTPS 地址' }
 if (url.length > 1024) errors.url = 'URL 不能超过 1024 个字符'
 if (!['GET', 'HEAD'].includes(input.method)) errors.method = '请选择 GET 或 HEAD'
 if (!Number.isSafeInteger(Number(input.intervalSeconds)) || Number(input.intervalSeconds) < 30 || Number(input.intervalSeconds) > 86400) errors.intervalSeconds = '检测间隔须为 30–86400 秒的整数'
 if (!Number.isSafeInteger(Number(input.timeoutMs)) || Number(input.timeoutMs) < 1000 || Number(input.timeoutMs) > 30000) errors.timeoutMs = '超时时间须为 1000–30000 毫秒的整数'
 if (typeof input.enabled !== 'boolean') errors.enabled = '启用状态无效'
 return { errors, value: { name, url, method: input.method, intervalSeconds: Number(input.intervalSeconds), timeoutMs: Number(input.timeoutMs), enabled: input.enabled } }
}
export function requireMonitor(input) { const result = validateMonitor(input); if (Object.keys(result.errors).length) throw new Error(Object.values(result.errors)[0]); return result.value }
