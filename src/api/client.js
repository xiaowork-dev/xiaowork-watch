// The browser holds only an in-memory session snapshot. The server owns authorization.
const configuredBase = import.meta.env?.VITE_API_BASE_URL || '/api'
const base = /^\/(?!\/)[^?#]*$/.test(configuredBase) ? configuredBase.replace(/\/$/, '') : '/api'
let session = { loading: true, authenticated: false, username: '', csrfToken: '', error: '' }
let sessionGeneration = 0
const subscribers = new Set()
export const getSession = () => ({ ...session })
export function subscribeSession(listener) { subscribers.add(listener); return () => subscribers.delete(listener) }
function publish(value) {
 session = { ...session, ...value }
 for (const listener of subscribers) listener(getSession())
}
function expireSession(message = '') {
 ++sessionGeneration
 publish({ loading: false, authenticated: false, username: '', csrfToken: '', error: message })
}
function acceptSession(value) {
 if (typeof value?.authenticated !== 'boolean') throw new Error('后端会话格式无效')
 if (value.authenticated && (typeof value.csrfToken !== 'string' || !value.csrfToken || typeof value.username !== 'string')) throw new Error('后端会话格式无效')
 publish({ loading: false, authenticated: value.authenticated, username: value.authenticated ? value.username : '', csrfToken: value.authenticated ? value.csrfToken : '', error: '' })
 return getSession()
}
export class ApiError extends Error {
 constructor(message, status = 0) { super(message); this.name = 'ApiError'; this.status = status }
}
export async function request(path, { method = 'GET', body, timeout = 10000, login = false } = {}) {
 const headers = { Accept: 'application/json' }
 if (method !== 'GET' && !login) {
  if (session.loading || !session.authenticated || !session.csrfToken) throw new ApiError('请先登录管理员后台', 401)
  headers['X-CSRF-Token'] = session.csrfToken
 }
 if (body !== undefined) headers['Content-Type'] = 'application/json'
 const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout)
 try {
  const response = await fetch(base + path, { method, headers, credentials: 'same-origin', signal: controller.signal, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) })
  if (response.status === 401 && !login) expireSession('登录已失效，请重新登录')
  let value
  try { value = await response.json() } catch { throw new ApiError('后端返回格式无效，请检查 API 服务是否已启动', response.status) }
  if (!response.ok || value?.code !== 0) throw new ApiError(value?.message || `请求失败（HTTP ${response.status}）`, response.status)
  if (!Object.prototype.hasOwnProperty.call(value, 'data')) throw new ApiError('后端返回格式无效', response.status)
  return value.data
 } catch (error) {
  if (error instanceof ApiError) throw error
  throw new ApiError(error.name === 'AbortError' ? '请求超时，请检查后端连接' : '无法连接监控服务，请确认后端已启动并检查网络')
 } finally { clearTimeout(timer) }
}
export const authApi = {
 async session() {
  const generation = sessionGeneration
  try { const value = await request('/auth/session'); return generation === sessionGeneration ? acceptSession(value) : getSession() }
  catch (error) { if (generation === sessionGeneration) expireSession(error.message); throw error }
 },
 async login(username, password) {
  const generation = ++sessionGeneration
  const value = await request('/auth/login', { method: 'POST', body: { username, password }, login: true })
  return generation === sessionGeneration ? acceptSession(value) : getSession()
 },
 async logout() {
  ++sessionGeneration
  try { return await request('/auth/logout', { method: 'POST', body: {} }) }
  finally { expireSession() }
 }
}
