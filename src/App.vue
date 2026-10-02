<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { Activity, Radio, Plus, ChevronRight, RefreshCw, Ellipsis, CircleHelp, Play, Pause, Pencil, Trash2, X, ChevronLeft, LoaderCircle, Check, AlertCircle, Globe2, Clock3, ShieldCheck, FileClock, Server, Network, LogIn, LogOut } from 'lucide-vue-next'
import { monitorsApi as api } from './api/monitors.js'
import { authApi, getSession, subscribeSession } from './api/client.js'
import { parseRoute, routePath } from './api/routes.js'
import { relativeTime } from './api/time.js'
import MonitorForm from './components/MonitorForm.vue'
import FleetView from './components/FleetView.vue'
import StatusBadge from './components/StatusBadge.vue'
import AdminLogin from './components/AdminLogin.vue'
const route = ref(parseRoute(window.location.pathname)), session = ref(getSession()), clock = ref(Date.now()), logoutBusy = ref(false)
const section = computed(() => route.value.section), selected = computed(() => section.value === 'web' ? route.value.id : null)
const canManage = computed(() => route.value.admin && !session.value.loading && session.value.authenticated)
const loginRequired = computed(() => route.value.admin && !session.value.authenticated)
const sectionTitle = computed(() => section.value === 'web' ? '网站监控' : section.value === 'vps' ? 'VPS 监控' : '测速目标')
const monitors = ref([]), loading = ref(true), pageError = ref(''), menuId = ref(null), formState = ref(null)
const history = ref({ records: [], total: 0, page: 1, size: 8 }), historyLoading = ref(false), historyError = ref(''), checking = ref([]), busyIds = ref([])
const toast = ref(null), deleteTarget = ref(null), deleting = ref(false), deleteError = ref(''), confirmDialog = ref(null), refreshTime = ref(null)
let toastTimer, pollTimer, unsubscribe, historyRequest = 0, listRequest = 0, alive = true
const summary = computed(() => ({ total: monitors.value.length, up: monitors.value.filter(m => m.enabled && m.lastStatus === 'UP').length, down: monitors.value.filter(m => m.enabled && m.lastStatus === 'DOWN').length, other: monitors.value.filter(m => !m.enabled || !m.lastStatus || m.lastStatus === 'UNKNOWN').length }))
const currentMonitor = computed(() => monitors.value.find(m => m.id === selected.value) || null)
const checkLabel = '立即检测'
const historyPages = computed(() => Math.max(1, Math.ceil(history.value.total / history.value.size)))
const at = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const since = value => relativeTime(value, clock.value)
const link = (value, id = null) => routePath(value, id, route.value.admin)
function setRoute(value, replace = false) {
 const path = routePath(value.section, value.id, value.admin)
 if (window.location.pathname !== path) window.history[replace ? 'replaceState' : 'pushState']({}, '', path)
 route.value = value
 menuId.value = null; formState.value = null; deleteTarget.value = null
}
function navigate(value) { setRoute({ section: value, id: null, admin: route.value.admin }) }
function enterAdmin() { setRoute({ section: 'web', id: null, admin: true }) }
function publicPage() { setRoute({ section: route.value.section, id: route.value.id, admin: false }) }
function popstate() { route.value = parseRoute(window.location.pathname); menuId.value = null; formState.value = null; deleteTarget.value = null }
function requireAdmin() { if (!canManage.value) throw new Error('请先登录管理员后台') }
function notify(message, error = false) { clearTimeout(toastTimer); toast.value = { message, error }; toastTimer = setTimeout(() => { toast.value = null }, 4500) }
async function logout() {
 if (logoutBusy.value) return
 logoutBusy.value = true
 try { await authApi.logout(); notify('已退出管理员后台') }
 catch (e) { notify('退出请求未完成：' + e.message, true) }
 finally { publicPage(); logoutBusy.value = false }
}
async function loadList() {
 if (loading.value && listRequest > 0) return
 const request = ++listRequest; loading.value = true; pageError.value = ''
 try { const value = await api.list(); if (alive && request === listRequest) { monitors.value = value; refreshTime.value = new Date() } }
 catch (error) { if (alive && request === listRequest) pageError.value = error.message }
 finally { if (alive && request === listRequest) loading.value = false }
}
async function refresh() { await loadList(); if (selected.value) await loadHistory(history.value.page) }
async function loadHistory(page = 1, background = false) {
 const id = selected.value
 if (!id || (background && historyLoading.value)) return
 const request = ++historyRequest; if (!background) historyLoading.value = true; historyError.value = ''
 try { const value = await api.history(id, page, 8); if (alive && request === historyRequest && selected.value === id) history.value = value }
 catch (error) { if (alive && request === historyRequest) historyError.value = error.message }
 finally { if (alive && request === historyRequest) historyLoading.value = false }
}
function openDetail(id) { setRoute({ section: 'web', id, admin: route.value.admin }) }
function goBack() { navigate('web'); nextTick(() => document.getElementById('list-title')?.focus()) }
function fleetDetail(id) { setRoute({ section: 'vps', id, admin: route.value.admin }) }
function openForm(monitor = null) { requireAdmin(); if (formState.value || deleteTarget.value) return; menuId.value = null; formState.value = { monitor } }
async function saveMonitor(value) {
 requireAdmin()
 const editId = formState.value?.monitor?.id
 if (editId) await api.update(editId, value); else await api.create(value)
 await loadList(); notify(editId ? '监控配置已更新' : '监控已创建，等待首次检测')
}
async function check(monitor) {
 if (!canManage.value || !monitor?.enabled || checking.value.includes(monitor.id) || busyIds.value.includes(monitor.id)) return
 menuId.value = null; checking.value = [...checking.value, monitor.id]
 try { const result = await api.check(monitor.id, monitor.timeoutMs); await loadList(); if (selected.value === monitor.id) await loadHistory(1); notify('检测完成：' + (result.success ? '成功' : result.errorMessage || '失败')) }
 catch (error) { notify(error.message, true) }
 finally { checking.value = checking.value.filter(id => id !== monitor.id) }
}
async function toggle(monitor) {
 if (!canManage.value || checking.value.includes(monitor.id) || busyIds.value.includes(monitor.id)) return
 menuId.value = null; busyIds.value.push(monitor.id)
 try { await api.setEnabled(monitor.id, !monitor.enabled); await loadList(); notify(monitor.enabled ? '监控已停用，最近检测结果已保留' : '监控已启用') }
 catch (error) { notify(error.message, true) }
 finally { busyIds.value = busyIds.value.filter(id => id !== monitor.id) }
}
async function askDelete(monitor) { requireAdmin(); if (formState.value || deleteTarget.value) return; menuId.value = null; deleteTarget.value = monitor; deleteError.value = ''; await nextTick(); confirmDialog.value?.showModal() }
function closeDelete() { if (!deleting.value) deleteTarget.value = null }
async function removeMonitor() {
 if (!canManage.value || deleting.value || !deleteTarget.value) return
 deleting.value = true; deleteError.value = ''
 try { const id = deleteTarget.value.id; await api.remove(id); if (selected.value === id) goBack(); deleteTarget.value = null; await loadList(); notify('监控及其检测历史已删除') }
 catch (error) { deleteError.value = error.message }
 finally { deleting.value = false }
}
function outsideClick(event) { if (!event.target.closest('.action-menu-container')) menuId.value = null }
function keydown(event) { if (event.key === 'Escape') menuId.value = null }
watch(selected, value => { ++historyRequest; historyLoading.value = false; history.value = { records: [], total: 0, page: 1, size: 8 }; if (value) loadHistory(); nextTick(() => document.getElementById(value ? 'detail-title' : 'list-title')?.focus()) }, { immediate: true })
watch(canManage, value => { if (!value) { menuId.value = null; formState.value = null; deleteTarget.value = null } })
watch([sectionTitle, currentMonitor, () => route.value.admin], () => { document.title = (route.value.admin ? '管理后台 · ' : '') + (currentMonitor.value?.name || sectionTitle.value) + ' · xiaowork Watch' }, { immediate: true })
onMounted(() => {
 unsubscribe = subscribeSession(value => { const wasAuthenticated = session.value.authenticated; session.value = value; if (wasAuthenticated && !value.authenticated && route.value.admin && !logoutBusy.value) { setRoute({ section: 'web', id: null, admin: true }, true) } })
 authApi.session().catch(() => {}); loadList()
 pollTimer = setInterval(() => { clock.value = Date.now(); if (section.value === 'web') { loadList(); if (selected.value) loadHistory(history.value.page, true) } if (canManage.value) authApi.session().catch(() => {}) }, 15000)
 window.addEventListener('popstate', popstate); document.addEventListener('click', outsideClick); document.addEventListener('keydown', keydown)
})
onUnmounted(() => { alive = false; ++listRequest; ++historyRequest; unsubscribe?.(); clearTimeout(toastTimer); clearInterval(pollTimer); window.removeEventListener('popstate', popstate); document.removeEventListener('click', outsideClick); document.removeEventListener('keydown', keydown) })
</script>
<template>
 <div class="app-shell">
  <aside class="sidebar">
   <a class="brand" :href="link('web')" @click.prevent="navigate('web')"><span class="brand-mark"><Activity :size="24" /></span><span>xiaowork<span class="brand-light">Watch</span></span></a>
   <div class="workspace-label">工作空间</div>
   <a :class="['nav-item',{active:section === 'web'}]" :href="link('web')" :aria-current="section === 'web' ? 'page' : undefined" @click.prevent="navigate('web')"><Radio :size="18" />网站监控<span class="nav-count">{{monitors.length}}</span></a>
   <a :class="['nav-item',{active:section === 'vps'}]" :href="link('vps')" :aria-current="section === 'vps' ? 'page' : undefined" @click.prevent="navigate('vps')"><Server :size="18" />VPS 监控</a>
   <a :class="['nav-item',{active:section === 'nodes'}]" :href="link('nodes')" :aria-current="section === 'nodes' ? 'page' : undefined" @click.prevent="navigate('nodes')"><Network :size="18" />测速目标</a>
   <div class="sidebar-bottom"><div class="version"><span>当前版本</span><b>v0.4.2</b></div><p>网站 · VPS · 测速目标</p><div class="account"><span class="avatar">X</span><div><b>{{canManage ? '管理员后台' : '公开状态页'}}</b><small>{{canManage ? session.username : '公开只读'}}</small></div></div></div>
  </aside>
  <div class="main-shell">
   <header class="topbar"><div class="breadcrumb"><span class="workspace-crumb">工作空间</span><ChevronRight :size="14" /><button @click="section === 'web' ? goBack() : navigate(section)">{{sectionTitle}}</button><template v-if="section === 'web' && currentMonitor"><ChevronRight :size="14" /><span>{{currentMonitor.name}}</span></template></div><div class="session-actions"><span class="access-badge">{{canManage ? '管理员' : '公开只读'}}</span><template v-if="canManage"><button class="button subtle" @click="publicPage">公开页</button><button class="button subtle" :disabled="logoutBusy" @click="logout"><LogOut :size="15" />退出登录</button></template><button v-else-if="!route.admin" class="button subtle" @click="enterAdmin"><LogIn :size="15" />进入后台</button><button v-else class="button subtle" @click="publicPage">返回公开页</button></div></header>
   <main>
    <div v-if="route.admin && session.loading" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在检查管理员会话…</p></div>
    <AdminLogin v-else-if="loginRequired" :session-error="session.error" @success="refresh" />
    <FleetView v-else-if="section !== 'web'" :key="section" :kind="section === 'vps' ? 'vps' : 'nodes'" :editable="canManage" :selected-id="route.id" @detail="fleetDetail" @back="navigate('vps')" />
    <template v-else>
    <div v-if="selected && !currentMonitor" class="empty-state"><AlertCircle :size="30" /><h3>{{loading ? '正在加载监控…' : '无法读取这个监控'}}</h3><p v-if="pageError" role="alert">{{pageError}}</p><button class="button" @click="goBack">返回列表</button></div>
    <template v-else-if="!currentMonitor">
     <div class="page-heading"><div><div class="eyebrow">SERVICE MONITORING</div><h1 id="list-title" tabindex="-1">监控列表<span>{{monitors.length}}</span></h1><p>在一个地方，掌握每个服务的当前状态。</p></div><button v-if="canManage" class="button primary" @click="openForm()"><Plus :size="18" />新增监控</button></div>
     <section class="status-summary" aria-label="监控状态汇总"><div><span class="summary-label">监控总数</span><strong>{{summary.total}}<small>项服务</small></strong></div><div><span class="summary-label"><i class="status-dot up"></i>运行正常</span><strong>{{summary.up}}<small>UP</small></strong></div><div><span class="summary-label"><i class="status-dot down"></i>检测失败</span><strong>{{summary.down}}<small>DOWN</small></strong></div><div><span class="summary-label"><i class="status-dot unknown"></i>未检测 / 已停用</span><strong>{{summary.other}}<small>项服务</small></strong></div></section>
     <section class="monitor-panel" :aria-busy="loading"><div class="panel-toolbar"><h2>所有监控<span>HTTP / HTTPS</span></h2><button class="button subtle" :disabled="loading" @click="refresh"><RefreshCw :size="15" :class="{spin:loading}" />{{loading ? '加载中…' : '刷新列表'}}</button></div>
      <div v-if="pageError" class="empty-state"><AlertCircle :size="32" /><h3>列表加载失败</h3><p role="alert">{{pageError}}</p><button class="button" @click="loadList">重试</button></div>
      <div v-else-if="loading && !monitors.length" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在加载监控…</p></div>
      <div v-else-if="!monitors.length" class="empty-state"><Radio :size="36" /><h3>暂无网站监控</h3><p>管理员添加监控后，真实检测结果会显示在这里。</p><button v-if="canManage" class="button primary" @click="openForm()"><Plus :size="16" />新增监控</button></div>
      <div v-else class="table-scroll"><table class="monitor-table"><thead><tr><th>监控名称</th><th>当前状态</th><th>HTTP 状态码</th><th>响应时间</th><th>最近检测</th><th class="align-right">操作</th></tr></thead><tbody><tr v-for="m in monitors" :key="m.id" :class="{ 'disabled-row': !m.enabled }"><td><div class="monitor-identity"><span :class="['service-icon', m.color || 'purple']">{{m.initial || m.name[0]}}</span><div><button class="monitor-name" @click="openDetail(m.id)">{{m.name}}<ChevronRight :size="14" /></button><span class="monitor-url" :title="m.url">{{m.url}}</span></div></div></td><td><StatusBadge :status="m.lastStatus" :enabled="m.enabled" /></td><td class="mono">{{m.lastHttpCode ?? '—'}}</td><td><span class="response-value mono">{{m.lastResponseTimeMs ?? '—'}}</span><span v-if="m.lastResponseTimeMs != null" class="muted"> ms</span></td><td class="muted" :title="at(m.lastCheckedAt)">{{since(m.lastCheckedAt)}}</td><td class="align-right"><div v-if="canManage" class="row-actions"><button class="icon-button" :aria-label="m.name + '：' + checkLabel" :title="checkLabel" :disabled="!m.enabled || checking.includes(m.id) || busyIds.includes(m.id)" @click="check(m)"><LoaderCircle v-if="checking.includes(m.id)" :size="17" class="spin" /><Play v-else :size="16" /></button><div class="action-menu-container"><button class="icon-button" :aria-label="m.name + '：更多操作'" aria-haspopup="menu" :aria-expanded="menuId === m.id" :disabled="checking.includes(m.id) || busyIds.includes(m.id)" @click.stop="menuId = menuId === m.id ? null : m.id"><Ellipsis :size="20" /></button><div v-if="menuId === m.id" class="action-menu" role="menu"><button role="menuitem" @click="openDetail(m.id)"><FileClock :size="15" />查看详情</button><button role="menuitem" @click="openForm(m)"><Pencil :size="15" />编辑监控</button><button role="menuitem" @click="toggle(m)"><Pause v-if="m.enabled" :size="15" /><Play v-else :size="15" />{{m.enabled ? '停用监控' : '启用监控'}}</button><button role="menuitem" class="danger-text" @click="askDelete(m)"><Trash2 :size="15" />删除监控</button></div></div></div><button v-else class="button subtle" @click="openDetail(m.id)">查看详情</button></td></tr></tbody></table></div>
      <div v-if="monitors.length && !pageError" class="table-footer"><span>共 {{monitors.length}} 个监控目标</span><span>列表更新时间 {{refreshTime?.toLocaleTimeString('zh-CN', {hour12: false}) || '—'}}</span></div>
     </section>
    </template>
    <template v-else>
     <button class="back-button" @click="goBack"><ChevronLeft :size="16" />返回监控列表</button>
     <div class="page-heading detail-heading"><div class="detail-identity"><span :class="['service-icon large', currentMonitor.color || 'purple']">{{currentMonitor.initial || currentMonitor.name[0]}}</span><div><h1 id="detail-title" tabindex="-1">{{currentMonitor.name}}</h1><div class="detail-url">{{currentMonitor.url}}</div></div></div><div v-if="canManage" class="detail-actions"><button class="button" :disabled="checking.includes(currentMonitor.id)" @click="openForm(currentMonitor)"><Pencil :size="16" />编辑</button><button class="button primary" :disabled="!currentMonitor.enabled || checking.includes(currentMonitor.id)" @click="check(currentMonitor)"><LoaderCircle v-if="checking.includes(currentMonitor.id)" :size="16" class="spin" /><Play v-else :size="16" />{{checking.includes(currentMonitor.id) ? '检测中…' : checkLabel}}</button></div></div>
     <p v-if="pageError" class="inline-error" role="alert">{{pageError}}；下方保留最后一次读取的数据。</p><section class="detail-overview"><div><span class="summary-label">当前状态</span><StatusBadge :status="currentMonitor.lastStatus" :enabled="currentMonitor.enabled" show-code /><small v-if="!currentMonitor.enabled">最近结果：{{currentMonitor.lastStatus || 'UNKNOWN'}}</small></div><div><span class="summary-label">HTTP 状态码</span><strong class="mono">{{currentMonitor.lastHttpCode ?? '—'}}</strong></div><div><span class="summary-label">最近响应时间</span><strong class="mono">{{currentMonitor.lastResponseTimeMs ?? '—'}}<small v-if="currentMonitor.lastResponseTimeMs != null"> ms</small></strong></div><div><span class="summary-label">最近检测</span><b class="detail-time">{{at(currentMonitor.lastCheckedAt)}}</b></div></section>
     <section class="config-strip" aria-label="监控配置"><span><Globe2 :size="16" />请求方法<b>{{currentMonitor.method}}</b></span><span><Clock3 :size="16" />检测间隔<b>{{currentMonitor.intervalSeconds}} 秒</b></span><span><ShieldCheck :size="16" />超时<b>{{currentMonitor.timeoutMs}} 毫秒</b></span><button v-if="canManage" :disabled="checking.includes(currentMonitor.id) || busyIds.includes(currentMonitor.id)" @click="toggle(currentMonitor)"><Pause v-if="currentMonitor.enabled" :size="14" /><Play v-else :size="14" />{{currentMonitor.enabled ? '停用监控' : '启用监控'}}</button></section>
     <section class="monitor-panel history-panel" :aria-busy="historyLoading"><div class="panel-toolbar"><h2>检测历史<span>最新记录优先</span></h2><button class="button subtle" :disabled="historyLoading" @click="loadHistory(history.page)"><RefreshCw :size="15" :class="{spin:historyLoading}" />刷新记录</button></div>
      <div v-if="historyError" class="empty-state"><AlertCircle :size="30" /><h3>历史加载失败</h3><p role="alert">{{historyError}}</p><button class="button" @click="loadHistory(history.page)">重试</button></div>
      <div v-else-if="historyLoading" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在加载检测记录…</p></div>
      <div v-else-if="!history.total" class="empty-state"><FileClock :size="35" /><h3>还没有检测记录</h3><p>{{currentMonitor.enabled ? '等待首次检测后，结果会出现在这里。' : '监控已停用，当前没有检测记录。'}}</p><button v-if="canManage" class="button" :disabled="!currentMonitor.enabled || checking.includes(currentMonitor.id)" @click="check(currentMonitor)"><Play :size="15" />{{checkLabel}}</button></div>
      <div v-else class="table-scroll"><table class="monitor-table history-table"><thead><tr><th>检测时间</th><th>检测结果</th><th>HTTP 状态码</th><th>响应时间</th><th>错误摘要</th></tr></thead><tbody><tr v-for="record in history.records" :key="record.id"><td class="mono">{{at(record.checkedAt)}}</td><td><span :class="['status-badge', record.success ? 'up' : 'down']"><Check v-if="record.success" :size="13" /><X v-else :size="13" />{{record.success ? '成功' : '失败'}}</span></td><td class="mono">{{record.httpCode ?? '—'}}</td><td class="mono">{{record.responseTimeMs ?? '—'}}<span v-if="record.responseTimeMs != null" class="muted"> ms</span></td><td><div v-if="record.errorType" class="history-error"><span>{{record.errorType}}</span><p>{{record.errorMessage || '未知错误'}}</p></div><span v-else class="muted">—</span></td></tr></tbody></table></div>
      <div v-if="history.total && !historyLoading && !historyError" class="table-footer"><span>共 {{history.total}} 条记录 · 每页 {{history.size}} 条</span><div class="pagination"><button class="icon-button" aria-label="上一页" :disabled="history.page <= 1" @click="loadHistory(history.page - 1)"><ChevronLeft :size="17" /></button><span>{{history.page}} / {{historyPages}}</span><button class="icon-button" aria-label="下一页" :disabled="history.page >= historyPages" @click="loadHistory(history.page + 1)"><ChevronRight :size="17" /></button></div></div>
     </section>
    </template>
    </template>
    <p v-if="!loginRequired" class="fleet-context"><CircleHelp :size="16" />每 15 秒刷新已有数据；刷新不会发起检测。{{canManage ? '管理操作由服务器校验登录权限。' : '当前为公开只读页面。'}}</p>
   </main>
   <footer class="app-footer"><span>xiaowork Watch</span><span>Website & VPS Monitor · v0.4.2</span></footer>
  </div>
  <MonitorForm v-if="canManage && formState" :monitor="formState.monitor" :on-save="saveMonitor" @close="formState = null" />
  <dialog v-if="canManage && deleteTarget" ref="confirmDialog" class="confirm-dialog" aria-labelledby="delete-title" @cancel.prevent="closeDelete"><div class="delete-icon"><Trash2 :size="24" /></div><h2 id="delete-title">删除这个监控？</h2><p>将删除「{{deleteTarget.name}}」及其检测历史，此操作无法撤销。</p><p v-if="deleteError" class="inline-error" role="alert">{{deleteError}}</p><div class="dialog-footer"><button class="button" :disabled="deleting" autofocus @click="closeDelete">取消</button><button class="button danger" :disabled="deleting" @click="removeMonitor"><LoaderCircle v-if="deleting" :size="16" class="spin" />{{deleting ? '删除中…' : '确认删除'}}</button></div></dialog>
  <div v-if="toast" :class="['toast', {error:toast.error}]" role="status"><AlertCircle v-if="toast.error" :size="18" /><Check v-else :size="18" /><span>{{toast.message}}</span><button class="icon-button" aria-label="关闭提示" @click="toast = null"><X :size="15" /></button></div>
 </div>
</template>
