<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { Activity, Radio, Plus, ChevronRight, RefreshCw, Ellipsis, CircleHelp, Play, Pause, Pencil, Trash2, X, ChevronLeft, LoaderCircle, Check, AlertCircle, Globe2, Clock3, ShieldCheck, FileClock, Server, Network } from 'lucide-vue-next'
import { monitorsApi as api, isDemo } from './api/monitors'
import MonitorForm from './components/MonitorForm.vue'
import FleetView from './components/FleetView.vue'
import StatusBadge from './components/StatusBadge.vue'
const section = ref('web')
const sectionTitle = computed(() => section.value === 'web' ? '网站监控' : section.value === 'vps' ? 'VPS 监控' : '测试节点')
function navigate(value) { section.value = value; goBack(); document.title = sectionTitle.value + ' · xiaowork Watch' }
const monitors = ref([]), loading = ref(true), pageError = ref(''), selected = ref(null), menuId = ref(null), formState = ref(null)
const history = ref({ records: [], total: 0, page: 1, size: 8 }), historyLoading = ref(false), historyError = ref(''), checking = ref([]), busyIds = ref([])
const toast = ref(null), deleteTarget = ref(null), deleting = ref(false), deleteError = ref(''), confirmDialog = ref(null), refreshTime = ref(null)
let toastTimer, historyRequest = 0, listRequest = 0
const summary = computed(() => ({ total: monitors.value.length, up: monitors.value.filter(m => m.enabled && m.lastStatus === 'UP').length, down: monitors.value.filter(m => m.enabled && m.lastStatus === 'DOWN').length, other: monitors.value.filter(m => !m.enabled || !m.lastStatus || m.lastStatus === 'UNKNOWN').length }))
const currentMonitor = computed(() => monitors.value.find(m => m.id === selected.value) || null)
const checkLabel = computed(() => isDemo ? '演示检测' : '立即检测')
const historyPages = computed(() => Math.max(1, Math.ceil(history.value.total / history.value.size)))
const at = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
function since(value) {
 if (!value) return '—'
 const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000))
 return seconds < 60 ? '刚刚' : seconds < 3600 ? Math.floor(seconds / 60) + ' 分钟前' : seconds < 86400 ? Math.floor(seconds / 3600) + ' 小时前' : Math.floor(seconds / 86400) + ' 天前'
}
function notify(message, error = false) { clearTimeout(toastTimer); toast.value = { message, error }; toastTimer = setTimeout(() => { toast.value = null }, 4500) }
async function loadList() {
 const request = ++listRequest
 loading.value = true; pageError.value = ''
 try { const value = await api.list(); if (request === listRequest) { monitors.value = value; refreshTime.value = new Date(); if (selected.value && !currentMonitor.value) selected.value = null } }
 catch (error) { if (request === listRequest) pageError.value = error.message }
 finally { if (request === listRequest) loading.value = false }
}
async function refresh() { await loadList(); if (currentMonitor.value) await loadHistory(history.value.page) }
async function loadHistory(page = 1) {
 const id = selected.value, request = ++historyRequest
 if (!id) return
 historyLoading.value = true; historyError.value = ''
 try { const value = await api.history(id, page, 8); if (request === historyRequest && selected.value === id) history.value = value }
 catch (error) { if (request === historyRequest) historyError.value = error.message }
 finally { if (request === historyRequest) historyLoading.value = false }
}
async function openDetail(id) { section.value = 'web'; menuId.value = null; selected.value = id; history.value = { records: [], total: 0, page: 1, size: 8 }; await loadHistory(); await nextTick(); document.getElementById('detail-title')?.focus() }
function goBack() { selected.value = null; ++historyRequest; historyLoading.value = false; nextTick(() => document.getElementById('list-title')?.focus()) }
function openForm(monitor = null) { section.value = 'web'; if (formState.value || deleteTarget.value) throw new Error('请先完成或关闭当前弹窗'); menuId.value = null; formState.value = { monitor } }
async function saveMonitor(value) {
 const editId = formState.value?.monitor?.id
 if (editId) await api.update(editId, value); else await api.create(value)
 await loadList(); notify(editId ? '监控配置已更新' : '监控已创建，等待首次检测')
}
async function check(monitor) {
 if (!monitor?.enabled || checking.value.includes(monitor.id) || busyIds.value.includes(monitor.id)) return
 menuId.value = null; checking.value = [...checking.value, monitor.id]
 try {
  const result = await api.check(monitor.id, monitor.timeoutMs); await loadList()
  if (selected.value === monitor.id) await loadHistory(1)
  notify(isDemo ? '演示完成：模拟返回 HTTP ' + result.httpCode + '，未请求实际地址' : '检测完成：' + (result.success ? '成功' : result.errorMessage || '失败'))
 } catch (error) { notify(error.message, true) }
 finally { checking.value = checking.value.filter(id => id !== monitor.id) }
}
async function toggle(monitor) {
 if (checking.value.includes(monitor.id) || busyIds.value.includes(monitor.id)) return
 menuId.value = null; busyIds.value.push(monitor.id)
 try { await api.setEnabled(monitor.id, !monitor.enabled); await loadList(); notify(monitor.enabled ? '监控已停用，最近检测结果已保留' : '监控已启用') }
 catch (error) { notify(error.message, true) }
 finally { busyIds.value = busyIds.value.filter(id => id !== monitor.id) }
}
async function askDelete(monitor) { menuId.value = null; deleteTarget.value = monitor; deleteError.value = ''; await nextTick(); confirmDialog.value.showModal() }
function closeDelete() { if (!deleting.value) deleteTarget.value = null }
async function removeMonitor() {
 if (deleting.value) return
 deleting.value = true; deleteError.value = ''
 try { const id = deleteTarget.value.id; await api.remove(id); if (selected.value === id) goBack(); deleteTarget.value = null; await loadList(); notify('监控及其检测历史已删除') }
 catch (error) { deleteError.value = error.message }
 finally { deleting.value = false }
}
function outsideClick(event) { if (!event.target.closest('.action-menu-container')) menuId.value = null }
function keydown(event) { if (event.key === 'Escape') menuId.value = null }
onMounted(() => { loadList(); document.addEventListener('click', outsideClick); document.addEventListener('keydown', keydown) })
onUnmounted(() => { clearTimeout(toastTimer); document.removeEventListener('click', outsideClick); document.removeEventListener('keydown', keydown) })
watch(selected, value => { document.title = value ? (currentMonitor.value?.name || '监控详情') + ' · xiaowork Watch' : 'xiaowork Watch · 服务监控' })
const webmcpLifecycle = new AbortController()
onUnmounted(() => webmcpLifecycle.abort())
onMounted(() => {
 const context = document.modelContext
 if (!context?.registerTool) return
 const tools = [
  { name: 'list_monitors', title: '读取监控列表', description: '读取当前示例或后端监控列表；示例数据不代表真实服务状态。', inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: true, untrustedContentHint: true }, execute: async input => { if (!input || Object.keys(input).length) throw new Error('不接受额外参数'); return { demo: isDemo, monitors: await api.list() } } },
  { name: 'open_monitor_detail', title: '打开监控详情', description: '在当前界面打开指定监控的配置和检测历史，不执行检测。', inputSchema: { type: 'object', properties: { monitorId: { type: 'integer', minimum: 1 } }, required: ['monitorId'], additionalProperties: false }, annotations: { readOnlyHint: true, untrustedContentHint: true }, execute: async input => { if (!input || !Number.isSafeInteger(input.monitorId) || input.monitorId < 1 || Object.keys(input).some(k => k !== 'monitorId')) throw new Error('monitorId 必须为正整数'); if (formState.value || deleteTarget.value) throw new Error('请先完成或关闭当前弹窗'); const monitor = await api.detail(input.monitorId); ++listRequest; loading.value = false; const index = monitors.value.findIndex(m => m.id === monitor.id); if (index < 0) monitors.value.push(monitor); else monitors.value[index] = monitor; await openDetail(monitor.id); await nextTick(); if (!currentMonitor.value || historyError.value) throw new Error(historyError.value || '无法打开监控详情'); return { demo: isDemo, monitorId: monitor.id, name: monitor.name, historyTotal: history.value.total } } },
  { name: 'start_monitor_creation', title: '打开新增监控表单', description: '仅打开新增监控表单，由用户填写并确认后保存。', inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: false, untrustedContentHint: false }, execute: async input => { if (!input || Object.keys(input).length) throw new Error('不接受额外参数'); openForm(); await nextTick(); return { opened: true, saved: false } } }
 ]
 for (const tool of tools) { try { Promise.resolve(context.registerTool(tool, { signal: webmcpLifecycle.signal })).catch(error => console.warn('WebMCP tool unavailable:', error.message)) } catch (error) { console.warn('WebMCP tool unavailable:', error.message) } }
})
</script>

<template>
 <div class="app-shell">
  <aside class="sidebar">
   <a class="brand" href="#" @click.prevent="navigate('web')"><span class="brand-mark"><Activity :size="24" /></span><span>xiaowork<span class="brand-light">Watch</span></span></a>
   <div class="workspace-label">工作空间</div>
   <a :class="['nav-item',{active:section === 'web'}]" href="#web" :aria-current="section === 'web' ? 'page' : undefined" @click.prevent="navigate('web')"><Radio :size="18" />网站监控<span class="nav-count">{{monitors.length}}</span></a>
   <a :class="['nav-item',{active:section === 'vps'}]" href="#vps" :aria-current="section === 'vps' ? 'page' : undefined" @click.prevent="navigate('vps')"><Server :size="18" />VPS 监控</a>
   <a :class="['nav-item',{active:section === 'nodes'}]" href="#nodes" :aria-current="section === 'nodes' ? 'page' : undefined" @click.prevent="navigate('nodes')"><Network :size="18" />测试节点</a>
   <div class="sidebar-bottom"><div class="version"><span>当前版本</span><b>v0.2 原型</b></div><p>网站 · VPS · 测试节点</p><div class="account"><span class="avatar">X</span><div><b>个人工作空间</b><small>xiaowork</small></div></div></div>
  </aside>
  <div class="main-shell">
   <header class="topbar"><div class="breadcrumb"><span class="workspace-crumb">工作空间</span><ChevronRight :size="14" /><button @click="section === 'web' ? goBack() : navigate(section)">{{sectionTitle}}</button><template v-if="section === 'web' && currentMonitor"><ChevronRight :size="14" /><span>{{currentMonitor.name}}</span></template></div><span v-if="isDemo" class="prototype-tag">界面原型 · 示例数据</span></header>
   <main>
    <FleetView v-if="section !== 'web'" :key="section" :kind="section === 'vps' ? 'vps' : 'nodes'" />
    <template v-else>
    <template v-if="!currentMonitor">
     <div class="page-heading"><div><div class="eyebrow">SERVICE MONITORING</div><h1 id="list-title" tabindex="-1">监控列表<span>{{monitors.length}}</span></h1><p>在一个地方，掌握每个服务的当前状态。</p></div><button class="button primary" @click="openForm()"><Plus :size="18" />新增监控</button></div>
     <section class="status-summary" aria-label="监控状态汇总"><div><span class="summary-label">监控总数</span><strong>{{summary.total}}<small>项服务</small></strong></div><div><span class="summary-label"><i class="status-dot up"></i>运行正常</span><strong>{{summary.up}}<small>UP</small></strong></div><div><span class="summary-label"><i class="status-dot down"></i>检测失败</span><strong>{{summary.down}}<small>DOWN</small></strong></div><div><span class="summary-label"><i class="status-dot unknown"></i>未检测 / 已停用</span><strong>{{summary.other}}<small>项服务</small></strong></div></section>
     <section class="monitor-panel" :aria-busy="loading"><div class="panel-toolbar"><h2>所有监控<span>HTTP / HTTPS</span></h2><button class="button subtle" :disabled="loading" @click="refresh"><RefreshCw :size="15" :class="{spin:loading}" />{{loading ? '加载中…' : '刷新列表'}}</button></div>
      <div v-if="pageError" class="empty-state"><AlertCircle :size="32" /><h3>列表加载失败</h3><p role="alert">{{pageError}}</p><button class="button" @click="loadList">重试</button></div>
      <div v-else-if="loading && !monitors.length" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在加载监控…</p></div>
      <div v-else-if="!monitors.length" class="empty-state"><Radio :size="36" /><h3>添加你的第一个监控</h3><p>从一个网站或 HTTP API 开始。</p><button class="button primary" @click="openForm()"><Plus :size="16" />新增监控</button></div>
      <div v-else class="table-scroll"><table class="monitor-table"><thead><tr><th>监控名称</th><th>当前状态</th><th>HTTP 状态码</th><th>响应时间</th><th>最近检测</th><th class="align-right">操作</th></tr></thead><tbody><tr v-for="m in monitors" :key="m.id" :class="{ 'disabled-row': !m.enabled }"><td><div class="monitor-identity"><span :class="['service-icon', m.color || 'purple']">{{m.initial || m.name[0]}}</span><div><button class="monitor-name" @click="openDetail(m.id)">{{m.name}}<ChevronRight :size="14" /></button><span class="monitor-url" :title="m.url">{{m.url}}</span></div></div></td><td><StatusBadge :status="m.lastStatus" :enabled="m.enabled" /></td><td class="mono">{{m.lastHttpCode ?? '—'}}</td><td><span class="response-value mono">{{m.lastResponseTimeMs ?? '—'}}</span><span v-if="m.lastResponseTimeMs != null" class="muted"> ms</span></td><td class="muted" :title="at(m.lastCheckedAt)">{{since(m.lastCheckedAt)}}</td><td class="align-right"><div class="row-actions"><button class="icon-button" :aria-label="m.name + '：' + checkLabel" :title="checkLabel" :disabled="!m.enabled || checking.includes(m.id) || busyIds.includes(m.id)" @click="check(m)"><LoaderCircle v-if="checking.includes(m.id)" :size="17" class="spin" /><Play v-else :size="16" /></button><div class="action-menu-container"><button class="icon-button" :aria-label="m.name + '：更多操作'" aria-haspopup="menu" :aria-expanded="menuId === m.id" :disabled="checking.includes(m.id) || busyIds.includes(m.id)" @click.stop="menuId = menuId === m.id ? null : m.id"><Ellipsis :size="20" /></button><div v-if="menuId === m.id" class="action-menu" role="menu"><button role="menuitem" @click="openDetail(m.id)"><FileClock :size="15" />查看详情</button><button role="menuitem" @click="openForm(m)"><Pencil :size="15" />编辑监控</button><button role="menuitem" @click="toggle(m)"><Pause v-if="m.enabled" :size="15" /><Play v-else :size="15" />{{m.enabled ? '停用监控' : '启用监控'}}</button><button role="menuitem" class="danger-text" @click="askDelete(m)"><Trash2 :size="15" />删除监控</button></div></div></div></td></tr></tbody></table></div>
      <div v-if="monitors.length && !pageError" class="table-footer"><span>共 {{monitors.length}} 个监控目标</span><span>列表更新时间 {{refreshTime?.toLocaleTimeString('zh-CN', {hour12: false}) || '—'}}</span></div>
     </section>
    </template>
    <template v-else>
     <button class="back-button" @click="goBack"><ChevronLeft :size="16" />返回监控列表</button>
     <div class="page-heading detail-heading"><div class="detail-identity"><span :class="['service-icon large', currentMonitor.color || 'purple']">{{currentMonitor.initial || currentMonitor.name[0]}}</span><div><h1 id="detail-title" tabindex="-1">{{currentMonitor.name}}</h1><div class="detail-url">{{currentMonitor.url}}</div></div></div><div class="detail-actions"><button class="button" :disabled="checking.includes(currentMonitor.id)" @click="openForm(currentMonitor)"><Pencil :size="16" />编辑</button><button class="button primary" :disabled="!currentMonitor.enabled || checking.includes(currentMonitor.id)" @click="check(currentMonitor)"><LoaderCircle v-if="checking.includes(currentMonitor.id)" :size="16" class="spin" /><Play v-else :size="16" />{{checking.includes(currentMonitor.id) ? '检测中…' : checkLabel}}</button></div></div>
     <section class="detail-overview"><div><span class="summary-label">当前状态</span><StatusBadge :status="currentMonitor.lastStatus" :enabled="currentMonitor.enabled" show-code /><small v-if="!currentMonitor.enabled">最近结果：{{currentMonitor.lastStatus || 'UNKNOWN'}}</small></div><div><span class="summary-label">HTTP 状态码</span><strong class="mono">{{currentMonitor.lastHttpCode ?? '—'}}</strong></div><div><span class="summary-label">最近响应时间</span><strong class="mono">{{currentMonitor.lastResponseTimeMs ?? '—'}}<small v-if="currentMonitor.lastResponseTimeMs != null"> ms</small></strong></div><div><span class="summary-label">最近检测</span><b class="detail-time">{{at(currentMonitor.lastCheckedAt)}}</b></div></section>
     <section class="config-strip" aria-label="监控配置"><span><Globe2 :size="16" />请求方法<b>{{currentMonitor.method}}</b></span><span><Clock3 :size="16" />检测间隔<b>{{currentMonitor.intervalSeconds}} 秒</b></span><span><ShieldCheck :size="16" />超时<b>{{currentMonitor.timeoutMs}} 毫秒</b></span><button :disabled="checking.includes(currentMonitor.id) || busyIds.includes(currentMonitor.id)" @click="toggle(currentMonitor)"><Pause v-if="currentMonitor.enabled" :size="14" /><Play v-else :size="14" />{{currentMonitor.enabled ? '停用监控' : '启用监控'}}</button></section>
     <section class="monitor-panel history-panel" :aria-busy="historyLoading"><div class="panel-toolbar"><h2>检测历史<span>最新记录优先</span></h2><button class="button subtle" :disabled="historyLoading" @click="loadHistory(history.page)"><RefreshCw :size="15" :class="{spin:historyLoading}" />刷新记录</button></div>
      <div v-if="historyError" class="empty-state"><AlertCircle :size="30" /><h3>历史加载失败</h3><p role="alert">{{historyError}}</p><button class="button" @click="loadHistory(history.page)">重试</button></div>
      <div v-else-if="historyLoading" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在加载检测记录…</p></div>
      <div v-else-if="!history.total" class="empty-state"><FileClock :size="35" /><h3>还没有检测记录</h3><p>{{currentMonitor.enabled ? '执行首次检测后，结果会出现在这里。' : '启用监控后，再执行首次检测。'}}</p><button class="button" :disabled="!currentMonitor.enabled || checking.includes(currentMonitor.id)" @click="check(currentMonitor)"><Play :size="15" />{{checkLabel}}</button></div>
      <div v-else class="table-scroll"><table class="monitor-table history-table"><thead><tr><th>检测时间</th><th>检测结果</th><th>HTTP 状态码</th><th>响应时间</th><th>错误摘要</th></tr></thead><tbody><tr v-for="record in history.records" :key="record.id"><td class="mono">{{at(record.checkedAt)}}</td><td><span :class="['status-badge', record.success ? 'up' : 'down']"><Check v-if="record.success" :size="13" /><X v-else :size="13" />{{record.success ? '成功' : '失败'}}</span></td><td class="mono">{{record.httpCode ?? '—'}}</td><td class="mono">{{record.responseTimeMs ?? '—'}}<span v-if="record.responseTimeMs != null" class="muted"> ms</span></td><td><div v-if="record.errorType" class="history-error"><span>{{record.errorType}}</span><p>{{record.errorMessage || '未知错误'}}</p></div><span v-else class="muted">—</span></td></tr></tbody></table></div>
      <div v-if="history.total && !historyLoading && !historyError" class="table-footer"><span>共 {{history.total}} 条记录 · 每页 {{history.size}} 条</span><div class="pagination"><button class="icon-button" aria-label="上一页" :disabled="history.page <= 1" @click="loadHistory(history.page - 1)"><ChevronLeft :size="17" /></button><span>{{history.page}} / {{historyPages}}</span><button class="icon-button" aria-label="下一页" :disabled="history.page >= historyPages" @click="loadHistory(history.page + 1)"><ChevronRight :size="17" /></button></div></div>
     </section>
    </template>
    </template>
    <div v-if="isDemo" class="demo-note"><CircleHelp :size="16" /><span>示例数据仅保存在当前浏览器。演示不会请求实际地址、发送 Ping 或安装探针，也不在后台定时运行。</span></div>
   </main>
   <footer class="app-footer"><span>xiaowork Watch</span><span>Website & VPS Monitor · v0.2 原型</span></footer>
  </div>
  <MonitorForm v-if="formState" :monitor="formState.monitor" :on-save="saveMonitor" @close="formState = null" />
  <dialog v-if="deleteTarget" ref="confirmDialog" class="confirm-dialog" aria-labelledby="delete-title" @cancel.prevent="closeDelete"><div class="delete-icon"><Trash2 :size="24" /></div><h2 id="delete-title">删除这个监控？</h2><p>将删除「{{deleteTarget.name}}」及其检测历史，此操作无法撤销。</p><p v-if="deleteError" class="inline-error" role="alert">{{deleteError}}</p><div class="dialog-footer"><button class="button" :disabled="deleting" autofocus @click="closeDelete">取消</button><button class="button danger" :disabled="deleting" @click="removeMonitor"><LoaderCircle v-if="deleting" :size="16" class="spin" />{{deleting ? '删除中…' : '确认删除'}}</button></div></dialog>
  <div v-if="toast" :class="['toast', {error:toast.error}]" role="status"><AlertCircle v-if="toast.error" :size="18" /><Check v-else :size="18" /><span>{{toast.message}}</span><button class="icon-button" aria-label="关闭提示" @click="toast = null"><X :size="15" /></button></div>
 </div>
</template>
