// Run with Node 20+: node test_frontend.mjs [frontend-or-repository-root]
import assert from 'node:assert/strict'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
const root = resolve(process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..'))
const mod = file => import(pathToFileURL(join(root, 'src/api', file)).href)
const { default: viteConfig } = await import(pathToFileURL(join(root, 'vite.config.js')).href)
const { request, authApi, getSession, subscribeSession } = await mod('client.js')
const { monitorsApi } = await mod('monitors.js')
const { fleetApi, validateFleet, normalizeAddress, parseTarget } = await mod('fleet.js')
const { targetConfigured, canMeasureHost, formatEndpoint, latestMeasurement, measurementProtocol, failureLabel, failureRate, measurementRtt, measurementLabel, directionLabel } = await mod('measurements.js')
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
 await fleetApi.create('vps', host); await fleetApi.update('vps', 1, host); await fleetApi.toggle('nodes', 2, false); await fleetApi.check(1); await fleetApi.installation('vps', 1); await fleetApi.remove('vps', 1)
 assert(calls.slice(before).every(call => call.options.method !== 'GET' && call.options.headers['X-CSRF-Token'] === 'fixture-csrf'))
 assert.equal(calls.at(-2).url, '/api/vps/1/enrollment')
})
await test('target CRUD splits TCP endpoints and never sends an enrollment request', async () => {
 respond({ id: 2 })
 const target = { name: 'Carrier', address: 'HB-CT-V4.ip.zstaticcdn.com:80', protocol: 'TCP', port: '', region: '华北', enabled: true }
 await fleetApi.create('nodes', target)
 assert.equal(calls.at(-1).url, '/api/probes')
 assert.deepEqual(JSON.parse(calls.at(-1).options.body), { name: 'Carrier', address: 'hb-ct-v4.ip.zstaticcdn.com', protocol: 'TCP', port: 80, region: '华北', enabled: true })
 await fleetApi.update('nodes', 2, { ...target, address: '1.1.1.1', protocol: 'ICMP', port: null })
 assert.equal(calls.at(-1).url, '/api/probes/2')
 assert.equal(JSON.parse(calls.at(-1).options.body).port, null)
 const before = calls.length
 await assert.rejects(fleetApi.installation('nodes', 2), /无需安装/)
 assert.equal(calls.length, before)
})
await test('HTTP write refusal retains the backend HTTPS instruction without a login loop', async () => {
 status = 403; response = { code: 1, message: '请先在服务器菜单8配置HTTPS，再登录后台。', data: null }
 await assert.rejects(fleetApi.check(1), /菜单8配置HTTPS/)
 assert.equal(getSession().authenticated, true)
 respond({})
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
await test('ICMP and TCP parse hostname, IPv4 and bracketed IPv6 without ambiguous ports', () => {
 assert.deepEqual(parseTarget(' Example.COM ', 'ICMP'), { address: 'example.com', protocol: 'ICMP', port: null })
 assert.deepEqual(parseTarget('1.1.1.1:443', 'TCP'), { address: '1.1.1.1', protocol: 'TCP', port: 443 })
 assert.deepEqual(parseTarget('example.com', 'TCP', 65535), { address: 'example.com', protocol: 'TCP', port: 65535 })
 assert.deepEqual(parseTarget('[2001:0DB8::1]:80', 'TCP', 80), { address: '2001:db8::1', protocol: 'TCP', port: 80 })
 assert.deepEqual(parseTarget('2001:db8::1', 'TCP', 443), { address: '2001:db8::1', protocol: 'TCP', port: 443 })
 assert.deepEqual(parseTarget('[2001:db8::1]', 'ICMP'), { address: '2001:db8::1', protocol: 'ICMP', port: null })
 assert.equal(formatEndpoint('2001:db8::1', 'TCP', 80), '[2001:db8::1]:80')
 assert.equal(formatEndpoint('example.com', 'ICMP', null), 'example.com')
})
await test('ICMP refuses explicit ports and TCP requires a matching bounded integer', () => {
 for (const [address, protocol, port] of [
  ['example.com:80', 'ICMP', null], ['[2001:db8::1]:80', 'ICMP', null], ['example.com', 'ICMP', 80],
  ['example.com', 'TCP', null], ['example.com:', 'TCP', null], ['example.com:80', 'TCP', 443],
  ['example.com', 'TCP', 0], ['example.com', 'TCP', 65536], ['example.com', 'TCP', 1.5],
  ['example.com', 'TCP', '80;id'], ['example.com', 'TCP', true], ['example.com', 'UDP', 80]
 ]) assert.throws(() => parseTarget(address, protocol, port), Error, `${address}/${protocol}/${port}`)
 const result = validateFleet({ name: 'Target', address: 'example.com:80', protocol: 'ICMP', port: null, region: '', enabled: true }, 'nodes')
 assert(result.errors.port)
})
await test('addresses reject URLs, malformed IP, shell-like text and hostname brackets', () => {
 for (const address of ['https://example.com', 'example.com/path', 'user@example.com', 'example.com?x=1', 'evil;id', 'bad\nhost', '999.1.1.1', '01.1.1.1', '2001:::1', '[example.com]', '[1.1.1.1]', '-host', 'host-', 'a..b', '中文.example', '2001:db8::1%eth0']) assert.throws(() => normalizeAddress(address), Error, address)
 assert.throws(() => parseTarget('[example.com]:80', 'TCP'), /IPv6/)
 const result = validateFleet({ name: 'Target', address: 'example.com', protocol: 'TCP', port: 443, region: '', enabled: true }, 'nodes')
 assert.deepEqual(result.errors, {})
 assert.deepEqual(result.value, { name: 'Target', address: 'example.com', protocol: 'TCP', port: 443, region: '', enabled: true })
 assert(validateFleet({ ...result.value, enabled: 'false' }, 'nodes').errors.enabled)
})
await test('legacy unconfigured targets stay unusable and old VPS agents cannot measure', () => {
 const now = Date.parse('2026-10-02T12:00:00Z')
 const target = { id: 2, address: 'example.com', protocol: 'TCP', port: 80, enabled: true, needsConfiguration: false }
 const host = { enabled: true, nodeIds: [2], agentVersion: 2, agentUpdateRequired: false, agentState: 'ONLINE', lastSeenAt: '2026-10-02T11:59:30Z' }
 assert.equal(targetConfigured(target), true)
 assert.equal(targetConfigured({ ...target, address: '', needsConfiguration: true }), false)
 assert.equal(targetConfigured({ ...target, port: null }), false)
 assert.equal(canMeasureHost(host, [target], now), true)
 for (const changes of [{ enabled: false }, { agentVersion: 1 }, { agentVersion: null }, { agentUpdateRequired: true }, { agentState: 'PENDING' }, { lastSeenAt: '2026-10-02T11:58:00Z' }, { nodeIds: [] }]) assert.equal(canMeasureHost({ ...host, ...changes }, [target], now), false)
 for (const changes of [{ enabled: false }, { address: '', needsConfiguration: true }]) assert.equal(canMeasureHost(host, [{ ...target, ...changes }], now), false)
})
await test('latest outbound measurement cannot mix legacy direction or edited target snapshots', () => {
 const target = { id: 2, address: 'new.example', protocol: 'TCP', port: 443 }
 const good = { id: 1, hostId: 1, nodeId: 2, direction: 'VPS_TO_TARGET', protocol: 'TCP', targetAddress: 'new.example', targetPort: 443, checkedAt: '2026-10-02T11:59:00Z' }
 const results = [good, ...[{ direction: 'NODE_TO_VPS' }, { targetAddress: 'old.example' }, { targetPort: 80 }, { protocol: 'ICMP' }, { hostId: 3 }, { nodeId: 3 }].map((change, index) => ({ ...good, ...change, id: index + 2, checkedAt: '2026-10-02T12:00:00Z' }))]
 assert.equal(latestMeasurement(results, 1, target), good)
 assert.equal(latestMeasurement(results.slice(1), 1, target), undefined)
 const snapshot = structuredClone(results)
 const newest = { ...good, id: 20, checkedAt: '2026-10-02T12:01:00Z' }
 assert.equal(latestMeasurement([...results, newest], 1, target), newest)
 assert.deepEqual(results, snapshot)
 assert.equal(directionLabel(good), 'VPS → 测速目标')
 assert.equal(directionLabel({ direction: 'NODE_TO_VPS' }), '旧版：测试节点 → VPS')
 assert.equal(measurementProtocol({ direction: 'NODE_TO_VPS' }), 'ICMP')
 assert.equal(formatEndpoint('', 'ICMP'), '未记录地址')
})
await test('TCP reports connection failure rate while ICMP reports packet loss', () => {
 const tcp = { protocol: 'TCP', direction: 'VPS_TO_TARGET', status: 'OK', sent: 5, received: 4, avgRttMs: 12.5 }
 const icmp = { ...tcp, protocol: 'ICMP' }
 assert.equal(failureLabel(tcp), '连接失败率'); assert.equal(failureLabel(icmp), '丢包率')
 assert.equal(failureRate(tcp), '20%'); assert.equal(failureRate(icmp), '20%')
 assert.equal(measurementLabel(tcp), '连接成功'); assert.equal(measurementLabel(icmp), '收到回复')
 assert.equal(measurementLabel({ ...tcp, status: 'TIMEOUT', received: 0 }), '连接失败')
 assert.equal(measurementLabel({ ...icmp, status: 'TIMEOUT', received: 0 }), 'ICMP 无回复')
 assert.equal(failureRate({ ...tcp, status: 'TIMEOUT', received: 0 }), '100%')
 assert.equal(measurementRtt({ ...tcp, avgRttMs: 0 }), 0)
 assert.equal(measurementRtt({ ...tcp, avgRttMs: null }), null)
})
await test('execution errors or invalid sample counts never fabricate 100 percent loss or RTT', () => {
 const failed = { protocol: 'TCP', status: 'ERROR', sent: 0, received: 0, avgRttMs: null, error: 'DNS resolution failed' }
 assert.equal(measurementLabel(failed), '测量失败')
 assert.equal(failureRate(failed), '—'); assert.equal(measurementRtt(failed), null)
 assert.equal(failureRate({ ...failed, sent: 5, received: 0 }), '—')
 for (const changes of [{ sent: 0 }, { sent: 1.5 }, { received: -1 }, { received: 6 }, { received: null }]) assert.equal(failureRate({ status: 'OK', sent: 5, received: 4, ...changes }), '—')
 for (const avgRttMs of [null, undefined, NaN, Infinity, -1]) assert.equal(measurementRtt({ status: 'OK', avgRttMs }), null)
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
