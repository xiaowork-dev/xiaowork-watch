export const THEME_STORAGE_KEY = 'xiaowork-watch-theme'
export const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)'

export function normalizeThemePreference(value) {
  return value === 'light' || value === 'dark' ? value : 'system'
}

export function resolveTheme(preference, systemDark) {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference
}

// Keep this function self-contained so the identical code can run in <head>
// before the stylesheet and the Vue application load.
export function bootstrapTheme(browser, document) {
  let preference = 'system'
  let systemDark = false
  try {
    const saved = browser.localStorage.getItem('xiaowork-watch-theme')
    if (saved === 'light' || saved === 'dark') preference = saved
  } catch {}
  try {
    systemDark = browser.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {}
  const theme = preference === 'system' ? (systemDark ? 'dark' : 'light') : preference
  document.documentElement.dataset.theme = theme
  document.documentElement.dataset.themePreference = preference
  document.documentElement.style.colorScheme = theme
}

export function themeBootstrapScript() {
  return `(${bootstrapTheme.toString()})(window, document);`
}

export function createThemeController(browser = globalThis.window, document = globalThis.document) {
  const listeners = new Set()
  let preference = 'system'
  let theme = 'light'
  let started = false
  let media = null
  let removeMediaListener = () => {}

  function savedPreference() {
    try {
      return normalizeThemePreference(browser.localStorage.getItem(THEME_STORAGE_KEY))
    } catch {
      return 'system'
    }
  }

  function getSnapshot() {
    return { preference, theme }
  }

  function apply(nextPreference) {
    const nextTheme = resolveTheme(nextPreference, Boolean(media?.matches))
    const changed = preference !== nextPreference || theme !== nextTheme
    preference = nextPreference
    theme = nextTheme
    if (document?.documentElement) {
      document.documentElement.dataset.theme = theme
      document.documentElement.dataset.themePreference = preference
      document.documentElement.style.colorScheme = theme
    }
    if (changed) {
      for (const listener of listeners) listener(getSnapshot())
    }
  }

  function onSystemChange() {
    if (preference === 'system') apply(preference)
  }

  function onStorage(event) {
    if (event.key !== THEME_STORAGE_KEY && event.key !== null) return
    // A sessionStorage change must not alter the saved localStorage choice.
    try {
      if (event.storageArea && event.storageArea !== browser.localStorage) return
    } catch {}
    apply(event.key === null ? savedPreference() : normalizeThemePreference(event.newValue))
  }

  function start() {
    if (started) return getSnapshot()
    started = true
    try {
      media = browser.matchMedia(THEME_MEDIA_QUERY)
    } catch {
      media = null
    }
    apply(savedPreference())
    if (media?.addEventListener) {
      media.addEventListener('change', onSystemChange)
      removeMediaListener = () => media.removeEventListener('change', onSystemChange)
    } else if (media?.addListener) {
      media.addListener(onSystemChange)
      removeMediaListener = () => media.removeListener(onSystemChange)
    }
    browser?.addEventListener('storage', onStorage)
    return getSnapshot()
  }

  function stop() {
    if (!started) return
    removeMediaListener()
    removeMediaListener = () => {}
    browser?.removeEventListener('storage', onStorage)
    media = null
    started = false
  }

  function setPreference(value) {
    if (!started) start()
    const nextPreference = normalizeThemePreference(value)
    // Storage can be unavailable in private browsing or a blocked origin.
    // The current page still switches immediately when persistence fails.
    try {
      browser.localStorage.setItem(THEME_STORAGE_KEY, nextPreference)
    } catch {}
    apply(nextPreference)
    return getSnapshot()
  }

  function subscribe(listener) {
    listeners.add(listener)
    listener(getSnapshot())
    return () => listeners.delete(listener)
  }

  return { start, stop, setPreference, getSnapshot, subscribe }
}

let controller

export function initTheme() {
  if (!controller) controller = createThemeController()
  controller.start()
  return controller
}

export function getThemeController() {
  return initTheme()
}

export function disposeTheme() {
  controller?.stop()
  controller = undefined
}
