export function parseRoute(pathname) {
 const admin = /^\/admin(?:\/|$)/.test(pathname)
 const path = admin ? pathname.slice(6) || '/' : pathname
 const monitor = path.match(/^\/monitors\/(\d+)\/?$/), host = path.match(/^\/vps\/(\d+)\/?$/)
 const id = monitor ? Number(monitor[1]) : host ? Number(host[1]) : null
 const safeId = Number.isSafeInteger(id) && id > 0 ? id : null
 return { admin, section: host || /^\/vps\/?$/.test(path) ? 'vps' : /^\/nodes\/?$/.test(path) ? 'nodes' : 'web', id: safeId }
}
export function routePath(section = 'web', id = null, admin = false) {
 const prefix = admin ? '/admin' : ''
 return prefix + (section === 'web' ? id ? `/monitors/${id}` : admin ? '' : '/' : `/${section}${id && section === 'vps' ? '/' + id : ''}`)
}
