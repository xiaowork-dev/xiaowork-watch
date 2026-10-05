// Keep a reader's chosen service stable as background refreshes reorder results.
export function sortMonitors(monitors) {
 return [...monitors].sort((a, b) => Number(b.enabled && b.lastStatus === 'DOWN') - Number(a.enabled && a.lastStatus === 'DOWN'))
}
export function resolveInspectionId(monitors, currentId) {
 return monitors.some(m => m.id === currentId) ? currentId : monitors[0]?.id ?? null
}
