import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import {
  THEME_STORAGE_KEY,
  bootstrapTheme,
  createThemeController,
  normalizeThemePreference,
  resolveTheme,
} from '../src/theme.js'

function environment({ saved, dark = false, blocked = false, writeBlocked = false, legacy = false, noMedia = false } = {}) {
  const values = new Map(saved === undefined ? [] : [[THEME_STORAGE_KEY, saved]])
  const mediaListeners = new Set()
  const storageListeners = new Set()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem(key, value) {
      if (writeBlocked) throw new Error('Storage quota exceeded')
      values.set(key, value)
    },
  }
  const media = {
    matches: dark,
    change(nextDark) {
      this.matches = nextDark
      for (const listener of mediaListeners) listener({ matches: nextDark })
    },
  }
  if (legacy) {
    media.addListener = (listener) => mediaListeners.add(listener)
    media.removeListener = (listener) => mediaListeners.delete(listener)
  } else {
    media.addEventListener = (type, listener) => {
      assert.equal(type, 'change')
      mediaListeners.add(listener)
    }
    media.removeEventListener = (type, listener) => {
      assert.equal(type, 'change')
      mediaListeners.delete(listener)
    }
  }
  const browser = {
    get localStorage() {
      if (blocked) throw new Error('Storage access denied')
      return storage
    },
    matchMedia(query) {
      assert.equal(query, '(prefers-color-scheme: dark)')
      if (noMedia) throw new Error('Media queries unavailable')
      return media
    },
    addEventListener(type, listener) {
      assert.equal(type, 'storage')
      storageListeners.add(listener)
    },
    removeEventListener(type, listener) {
      assert.equal(type, 'storage')
      storageListeners.delete(listener)
    },
  }
  const document = { documentElement: { dataset: {}, style: {} } }
  return {
    browser, document, media, storage, values, mediaListeners, storageListeners,
    storageChange(key, newValue, storageArea = storage) {
      for (const listener of storageListeners) listener({ key, newValue, storageArea })
    },
  }
}

function assertApplied(env, preference, theme) {
  assert.deepEqual(env.document.documentElement.dataset, { theme, themePreference: preference })
  assert.equal(env.document.documentElement.style.colorScheme, theme)
}

test('fresh visitors follow the system theme without a saved setting', () => {
  const env = environment({ dark: true })
  const controller = createThemeController(env.browser, env.document)
  assert.deepEqual(controller.start(), { preference: 'system', theme: 'dark' })
  assertApplied(env, 'system', 'dark')
  controller.stop()
})

test('saved manual preference wins over the system theme', () => {
  for (const saved of ['light', 'dark']) {
    const env = environment({ saved, dark: saved === 'light' })
    const controller = createThemeController(env.browser, env.document)
    controller.start()
    assertApplied(env, saved, saved)
    controller.stop()
  }
})

test('invalid or removed preferences return to system mode', () => {
  for (const saved of ['invalid', '', 'DARK', null, undefined]) {
    const env = environment({ saved, dark: true })
    const controller = createThemeController(env.browser, env.document)
    controller.start()
    assertApplied(env, 'system', 'dark')
    controller.stop()
  }
})

test('system changes update system mode live and notify subscribers', () => {
  const env = environment()
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  const snapshots = []
  const unsubscribe = controller.subscribe((snapshot) => snapshots.push(snapshot))
  env.media.change(true)
  env.media.change(false)
  assert.deepEqual(snapshots, [
    { preference: 'system', theme: 'light' },
    { preference: 'system', theme: 'dark' },
    { preference: 'system', theme: 'light' },
  ])
  assertApplied(env, 'system', 'light')
  unsubscribe()
  env.media.change(true)
  assert.equal(snapshots.length, 3)
  controller.stop()
})

test('manual themes stay fixed while the operating system changes', () => {
  const env = environment()
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  controller.setPreference('light')
  env.media.change(true)
  assertApplied(env, 'light', 'light')
  controller.setPreference('dark')
  env.media.change(false)
  assertApplied(env, 'dark', 'dark')
  controller.setPreference('system')
  assertApplied(env, 'system', 'light')
  controller.stop()
})

test('choices persist across refresh and system mode is saved explicitly', () => {
  const env = environment({ dark: true })
  const controller = createThemeController(env.browser, env.document)
  controller.setPreference('light')
  assert.equal(env.values.get(THEME_STORAGE_KEY), 'light')
  controller.stop()
  const refreshed = createThemeController(env.browser, env.document)
  assert.deepEqual(refreshed.start(), { preference: 'light', theme: 'light' })
  refreshed.setPreference('system')
  assert.equal(env.values.get(THEME_STORAGE_KEY), 'system')
  assertApplied(env, 'system', 'dark')
  refreshed.stop()
})

test('blocked storage still permits switching and live system following', () => {
  const env = environment({ blocked: true, dark: true })
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  controller.setPreference('light')
  assertApplied(env, 'light', 'light')
  controller.setPreference('system')
  env.media.change(false)
  assertApplied(env, 'system', 'light')
  controller.stop()
})

test('a storage write failure does not stop the current page switching', () => {
  const env = environment({ saved: 'light', writeBlocked: true })
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  controller.setPreference('dark')
  assertApplied(env, 'dark', 'dark')
  assert.equal(env.values.get(THEME_STORAGE_KEY), 'light')
  controller.stop()
})

test('other tabs synchronize choices, invalid values, removal and clearing', () => {
  const env = environment({ dark: true })
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  env.storageChange(THEME_STORAGE_KEY, 'light')
  assertApplied(env, 'light', 'light')
  env.storageChange(THEME_STORAGE_KEY, 'dark')
  assertApplied(env, 'dark', 'dark')
  env.storageChange(THEME_STORAGE_KEY, 'invalid')
  assertApplied(env, 'system', 'dark')
  env.storageChange(THEME_STORAGE_KEY, 'light')
  env.storageChange(THEME_STORAGE_KEY, null)
  assertApplied(env, 'system', 'dark')
  env.storageChange(THEME_STORAGE_KEY, 'light')
  env.values.clear()
  env.storageChange(null, null)
  assertApplied(env, 'system', 'dark')
  controller.stop()
})

test('unrelated keys and session storage events do not change theme', () => {
  const env = environment({ saved: 'light', dark: true })
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  env.storageChange('another-setting', 'dark')
  env.storageChange(THEME_STORAGE_KEY, 'dark', {})
  assertApplied(env, 'light', 'light')
  controller.stop()
})

test('starting twice does not duplicate listeners and stopping cleans up', () => {
  const env = environment()
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  controller.start()
  assert.equal(env.mediaListeners.size, 1)
  assert.equal(env.storageListeners.size, 1)
  controller.stop()
  controller.stop()
  assert.equal(env.mediaListeners.size, 0)
  assert.equal(env.storageListeners.size, 0)
  env.media.change(true)
  env.storageChange(THEME_STORAGE_KEY, 'dark')
  assertApplied(env, 'system', 'light')
  controller.start()
  assert.equal(env.mediaListeners.size, 1)
  assert.equal(env.storageListeners.size, 1)
  assertApplied(env, 'system', 'dark')
  controller.stop()
})

test('older matchMedia listener APIs follow the system and clean up', () => {
  const env = environment({ legacy: true })
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  assert.equal(env.mediaListeners.size, 1)
  env.media.change(true)
  assertApplied(env, 'system', 'dark')
  controller.stop()
  assert.equal(env.mediaListeners.size, 0)
})

test('unavailable media queries fall back to light and allow manual dark', () => {
  const env = environment({ noMedia: true })
  const controller = createThemeController(env.browser, env.document)
  controller.start()
  assertApplied(env, 'system', 'light')
  controller.setPreference('dark')
  assertApplied(env, 'dark', 'dark')
  controller.stop()
})

test('early head bootstrap and the controller resolve all preferences consistently', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8')
  const script = html.match(/<script\b[^>]*\bdata-theme-bootstrap\b[^>]*>([\s\S]*?)<\/script>/i)?.[1]
  assert.ok(script, 'index.html must initialize theme synchronously in <head>')
  assert.ok(html.indexOf('data-theme-bootstrap') < html.indexOf('</head>'))
  for (const saved of [undefined, null, '', 'invalid', 'system', 'light', 'dark']) {
    for (const dark of [false, true]) {
      for (const blocked of [false, true]) {
        const env = environment({ saved, dark, blocked })
        vm.runInNewContext(script, { window: env.browser, document: env.document })
        const expectedPreference = normalizeThemePreference(blocked ? null : saved)
        const expectedTheme = resolveTheme(expectedPreference, dark)
        assertApplied(env, expectedPreference, expectedTheme)
        const early = { ...env.document.documentElement.dataset }
        const controller = createThemeController(env.browser, env.document)
        controller.start()
        assert.deepEqual(env.document.documentElement.dataset, early)
        controller.stop()
      }
    }
  }
})

test('head bootstrap is resilient to unavailable media queries', () => {
  const env = environment({ blocked: true, noMedia: true })
  bootstrapTheme(env.browser, env.document)
  assertApplied(env, 'system', 'light')
})
