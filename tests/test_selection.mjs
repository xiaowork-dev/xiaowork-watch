import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import { resolve, join } from 'node:path'
const root = resolve(process.argv[2] || '.')
const { sortMonitors, resolveInspectionId } = await import(pathToFileURL(join(root, 'src/api/selection.js')).href)
const list = [{ id: 1, enabled: true, lastStatus: 'UP' }, { id: 2, enabled: false, lastStatus: 'DOWN' }, { id: 3, enabled: true, lastStatus: 'DOWN' }, { id: 4, enabled: true, lastStatus: 'UNKNOWN' }]
const sorted = sortMonitors(list)
assert.deepEqual(sorted.map(m => m.id), [3, 1, 2, 4])
assert.deepEqual(list.map(m => m.id), [1, 2, 3, 4], 'sorting must not mutate API data')
assert.equal(resolveInspectionId(sorted, null), 3, 'first load highlights an enabled failure')
assert.equal(resolveInspectionId(sorted, 1), 1, 'a refreshed failure must not steal the chosen service')
assert.equal(resolveInspectionId(sorted.filter(m => m.id !== 1), 1), 3, 'deleted service falls back to a remaining service')
assert.equal(resolveInspectionId([], 1), null, 'empty state must not retain a deleted inspector')
assert.equal(resolveInspectionId([{ id: 5, enabled: false }], null), 5, 'paused-only lists remain inspectable')
console.log('7 website selection and refresh checks passed')
