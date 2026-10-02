// Run with Node 20+: node test_frontend.mjs [frontend-or-repository-root]
import assert from 'node:assert/strict'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
const root = resolve(process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..'))
const mod = file => import(pathToFileURL(join(root, 'src/api', file)).href)
const { default: viteConfig } = await import(pathToFileURL(join(root, 'vite.config.js')).href)
const { request, authApi, getSession, subscribeSession } = await mod('client.js')
const { monitorsApi } = await mod('monitors.js')
const { fleetApi, validateFleet } = await mod('fleet.js')
const { validateMonitor } = await mod('model.js')
const { parseRoute, routePath } = await mod('routes.js')
const { agentState, resultIsStale, relativeTime } = await mod('time.js')
let calls = [], changes = [], response = { code: 0, message: '', data: {} }, status = 200
const unsubscribe = subscribeSession(value => changes.push(value))
globalThis.fetch = async (url, options) => { calls.push({ url, options }); return { ok: status >= 200 && status < 300, status, json: async () => response } }
const respond = data => { status = 200; response = { code: 0, message: '', data } }
let tests = 0
async function test(name, work) { await work(); tests++; console.log(`ok ${tests} - ${name}`) }
await test('development proxy preserves browser Origin and Host for login', () => {
 for (const prefix of ['/api', '/agent']) {
  assert.equal(viteConfig.server.proxy[prefix].changeOrigin, false)
  assert.equal(viteConfig.server.proxy[prefix].target, 'http://127.0.0.1:8091')
 }
})
const monitor = { name: 'Site', url: 'https://example.com', method: 'GET', intervalSeconds: 30, timeoutMs: 1000, enabled: true }
await test('anonymous public refresh uses only GET with same-origin credentials', async () => {
 respond([]); await monitorsApi.list(); respond({ hosts: [], nodes: [], results: [] }); await fleetApi.list()
 assert(calls.every(call => call.options.method === 'GET' && call.options.credentials === 'same-origin'))
 assert(calls.every(call => !call.options.headers['X-CSRF-Token']))
})
await test('anonymous mutations are refused before fetching', async () => {
 const before = calls.length
 await assert.rejects(monitorsApi.create(monitor), /登录/)
 await assert.rejects(fleetApi.installation('vps', 1), /登录/)
 assert.equal(calls.length, before)
})
await test('login accepts real backend session and holds token in memory', async () => {
 respond({ authenticated: true, username: 'admin', csrfToken: 'fixture-csrf' })
 await authApi.login('admin', 'fixture-only')
 const call = calls.at(-1)
 assert.equal(call.url, '/api/auth/login'); assert.equal(call.options.method, 'POST')
 assert.equal(call.options.headers['X-CSRF-Token'], undefined)
 assert.deepEqual(JSON.parse(call.options.body), { username: 'admin', password: 'fixture-only' })
 assert.equal(getSession().csrfToken, 'fixture-csrf'); assert.equal(changes.at(-1).authenticated, true)
})
await test('every monitor and fleet mutation sends CSRF', async () => {
 const before = calls.length; respond({ id: 1 })
 await monitorsApi.create(monitor); await monitorsApi.update(1, monitor); await monitorsApi.setEnabled(1, false); await monitorsApi.check(1, 1000); await monitorsApi.remove(1)
 const host = { name: 'VPS', address: '203.0.113.10', region: '', enabled: true, nodeIds: [2] }
 await fleetApi.create('vps', host); await fleetApi.update('vps', 1, host); await fleetApi.toggle('nodes', 2, false); await fleetApi.check(1); await fleetApi.installation('nodes', 2); await fleetApi.remove('vps', 1)
 assert(calls.slice(before).every(call => call.options.method !== 'GET' && call.options.headers['X-CSRF-Token'] === 'fixture-csrf'))
 assert.equal(calls.at(-2).url, '/api/probes/2/enrollment')
})
await test('history preserves pagination and never schedules a check', async () => {
 const before = calls.length; respond({ records: [], page: 2, size: 8, total: 9 })
 await monitorsApi.history(1, 2); await fleetApi.history(1, 2)
 assert(calls.slice(before).every(call => call.options.method === 'GET' && call.url.endsWith('/checks?page=2&size=8')))
})
await test('401 expires session and prevents further management', async () => {
 status = 401; response = { code: 1, message: 'Expired', data: null }
 await assert.rejects(fleetApi.check(1), /Expired/)
 assert.equal(getSession().authenticated, false); assert.equal(getSession().csrfToken, '')
 const before = calls.length; await assert.rejects(monitorsApi.remove(1), /登录/); assert.equal(calls.length, before)
})
await test('401 malformed response still invalidates session', async () => {
 respond({ authenticated: true, username: 'admin', csrfToken: 'fixture-2' }); await authApi.login('admin', 'fixture')
 globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => { throw new SyntaxError('html') } })
 await assert.rejects(request('/monitors'), /格式无效/); assert.equal(getSession().authenticated, false)
})
await test('network failure reports connection error and produces no demo fallback', async () => {
 globalThis.fetch = async () => { throw new TypeError('fetch failed') }
 await assert.rejects(monitorsApi.list(), /无法连接监控服务/)
 await assert.rejects(authApi.session(), /无法连接监控服务/)
 assert.equal(getSession().authenticated, false)
})
await test('logout clears session even when backend is unreachable', async () => {
 globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ code: 0, data: { authenticated: true, username: 'admin', csrfToken: 'fixture' } }) })
 await authApi.login('admin', 'fixture')
 globalThis.fetch = async () => { throw new TypeError('offline') }
 await assert.rejects(authApi.logout(), /无法连接/); assert.equal(getSession().csrfToken, '')
})
await test('invalid backend session cannot grant administrator access', async () => {
 globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ code: 0, data: { authenticated: true, username: 'admin' } }) })
 await assert.rejects(authApi.session(), /会话格式无效/); assert.equal(getSession().authenticated, false)
})
await test('a delayed session response cannot undo logout', async () => {
 const authenticated = { authenticated: true, username: 'admin', csrfToken: 'fixture' }
 globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ code: 0, data: authenticated }) })
 await authApi.login('admin', 'fixture')
 let finish
 globalThis.fetch = async url => url.endsWith('/auth/session') ? new Promise(resolve => { finish = () => resolve({ ok: true, status: 200, json: async () => ({ code: 0, data: authenticated }) }) }) : { ok: true, status: 200, json: async () => ({ code: 0, data: {} }) }
 const pending = authApi.session()
 await authApi.logout(); finish(); await pending
 assert.equal(getSession().authenticated, false); assert.equal(getSession().csrfToken, '')
})
await test('monitor bounds match backend and enabled is a boolean', () => {
 assert.deepEqual(validateMonitor(monitor).errors, {})
 for (const value of [29, 86401, 30.5]) assert(validateMonitor({ ...monitor, intervalSeconds: value }).errors.intervalSeconds)
 for (const value of [999, 30001, 1000.5]) assert(validateMonitor({ ...monitor, timeoutMs: value }).errors.timeoutMs)
 assert(validateMonitor({ ...monitor, enabled: 'false' }).errors.enabled)
 assert(validateMonitor({ ...monitor, url: 'javascript:alert(1)' }).errors.url)
})
await test('fleet rejects invalid addresses and node identifiers', () => {
 const value = { name: 'VPS', region: '', enabled: true, address: '203.0.113.10', nodeIds: [1, 1] }
 assert.deepEqual(validateFleet(value, 'vps').value.nodeIds, [1])
 for (const address of ['https://example.com', 'evil/host', '999.1.1.1', 'host:22', '-host']) assert(validateFleet({ ...value, address }, 'vps').errors.address)
 assert(validateFleet({ ...value, nodeIds: ['1'] }, 'vps').errors.nodeIds)
 assert.deepEqual(validateFleet({ ...value, address: '2001:db8::1' }, 'vps').errors, {})
})
await test('public/admin deep links survive pathname round trips', () => {
 for (const admin of [true, false]) for (const [section, id] of [['web', null], ['web', 17], ['vps', null], ['vps', 8], ['nodes', null]]) assert.deepEqual(parseRoute(routePath(section, id, admin)), { section, id, admin })
 assert.equal(parseRoute('/administrator').admin, false)
 assert.equal(parseRoute('/monitors/9007199254740992').id, null)
})
await test('heartbeat expires without inventing Ping failures or online states', () => {
 const now = Date.parse('2026-10-02T12:00:00Z')
 assert.equal(agentState({ agentState: 'ONLINE', lastSeenAt: '2026-10-02T11:59:00Z' }, now), 'ONLINE')
 assert.equal(agentState({ agentState: 'ONLINE', lastSeenAt: '2026-10-02T11:58:29Z' }, now), 'OFFLINE')
 assert.equal(agentState({ agentState: 'ONLINE', lastSeenAt: '2026-10-02T11:56:59Z' }, now), 'OFFLINE')
 assert.equal(agentState({ agentState: 'ONLINE', lastSeenAt: null }, now), 'OFFLINE')
 assert.equal(agentState({ agentState: 'PENDING', lastSeenAt: null }, now), 'PENDING')
 assert.equal(resultIsStale({ checkedAt: '2026-10-02T11:56:00Z', status: 'OK' }, now), true)
 assert.equal(relativeTime('2026-10-02T11:59:00Z', now), '1 分钟前')
 assert.equal(relativeTime('2026-10-02T11:59:00Z', now + 60000), '2 分钟前')
})
unsubscribe()
console.log(`${tests} frontend checks passed`)
