export function validateMonitor(input) {
 const errors = {}, name = typeof input.name === 'string' ? input.name.trim() : '', url = typeof input.url === 'string' ? input.url.trim() : ''
 if (!name) errors.name = '请输入监控名称'
 else if (name.length > 100) errors.name = '名称不能超过 100 个字符'
 try { if (!['https:', 'http:'].includes(new URL(url).protocol)) throw new Error() } catch { errors.url = '请输入有效的 HTTP 或 HTTPS 地址' }
 if (url.length > 1024) errors.url = 'URL 不能超过 1024 个字符'
 if (!['GET', 'HEAD'].includes(input.method)) errors.method = '请选择 GET 或 HEAD'
 for (const [key, label] of [['intervalSeconds', '检测间隔'], ['timeoutMs', '超时时间']]) if (!Number.isSafeInteger(Number(input[key])) || Number(input[key]) <= 0) errors[key] = `${label}必须是正整数`
 return { errors, value: { name, url, method: input.method, intervalSeconds: Number(input.intervalSeconds), timeoutMs: Number(input.timeoutMs), enabled: Boolean(input.enabled) } }
}
export function requireMonitor(input) { const result = validateMonitor(input); if (Object.keys(result.errors).length) throw new Error(Object.values(result.errors)[0]); return result.value }
