<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { Server, Network, Plus, RefreshCw, LoaderCircle, AlertCircle, ChevronLeft, ChevronRight, Pencil, Pause, Play, Trash2, Terminal, CircleHelp, Check, X } from 'lucide-vue-next'
import { fleetApi as api } from '../api/fleet.js'
import { agentState, resultIsStale } from '../api/time.js'
import FleetForm from './FleetForm.vue'
import InstallDialog from './InstallDialog.vue'
const props = defineProps({ kind: String, editable: Boolean, selectedId: Number })
const emit = defineEmits(['detail', 'back'])
const fleet = ref({ hosts: [], nodes: [], results: [] }), loading = ref(true), error = ref(''), formState = ref(null), installing = ref(null), deleting = ref(null), confirmDialog = ref(null), busy = ref(false), deleteError = ref(''), toast = ref(null)
const history = ref({ records: [], total: 0, page: 1, size: 8 }), historyLoading = ref(false), historyError = ref(''), clock = ref(Date.now())
let toastTimer, pollTimer, pendingInstall, alive = true, request = 0, historyRequest = 0
const title = computed(() => props.kind === 'vps' ? 'VPS 监控' : '测试节点')
const list = computed(() => props.kind === 'vps' ? fleet.value.hosts : fleet.value.nodes)
const current = computed(() => props.kind === 'vps' ? fleet.value.hosts.find(h => h.id === props.selectedId) : null)
const state = item => agentState(item, clock.value)
const summary = computed(() => ({ total: list.value.length, online: list.value.filter(x => state(x) === 'ONLINE').length, offline: list.value.filter(x => state(x) === 'OFFLINE').length, pending: list.value.filter(x => state(x) === 'PENDING').length }))
const selectedNodes = computed(() => fleet.value.nodes.filter(n => current.value?.nodeIds.includes(n.id)))
const usableNodes = computed(() => selectedNodes.value.filter(n => n.enabled && state(n) === 'ONLINE'))
const historyPages = computed(() => Math.max(1, Math.ceil(history.value.total / history.value.size)))
const stateLabel = item => state(item) === 'ONLINE' ? '在线' : state(item) === 'OFFLINE' ? '离线' : '待安装'
const stateClass = item => state(item) === 'ONLINE' ? 'up' : state(item) === 'OFFLINE' ? 'down' : 'unknown'
const at = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const loss = result => result?.status !== 'ERROR' && result?.sent > 0 ? Math.round((result.sent - result.received) / result.sent * 100) + '%' : '—'
const rtt = result => result?.status === 'ERROR' ? null : result?.avgRttMs
const measurementLabel = result => result.status === 'OK' ? '收到回复' : result.status === 'TIMEOUT' ? 'ICMP 无回复' : '测量失败'
const latest = node => fleet.value.results.filter(r => r.hostId === current.value?.id && r.nodeId === node.id).sort((a, b) => new Date(b.checkedAt) - new Date(a.checkedAt))[0]
const stale = result => resultIsStale(result, clock.value)
function notify(message, failed = false) { clearTimeout(toastTimer); toast.value = { message, failed }; toastTimer = setTimeout(() => { toast.value = null }, 4500) }
function requireAdmin() { if (!props.editable) throw new Error('请先登录管理员后台') }
function openForm(item = null) { requireAdmin(); if (!busy.value && !formState.value && !installing.value && !deleting.value) formState.value = { item } }
function install(item) { requireAdmin(); if (!busy.value && !formState.value && !installing.value && !deleting.value) installing.value = item }
async function load() {
 if (loading.value && request > 0) return
 const id = ++request; loading.value = true; error.value = ''
 try { const data = await api.list(); if (alive && id === request) fleet.value = data }
 catch (e) { if (alive && id === request) error.value = e.message }
 finally { if (alive && id === request) loading.value = false }
}
async function loadHistory(page = 1, background = false) {
 const hostId = props.selectedId
 if (!hostId || (background && historyLoading.value)) return
 const id = ++historyRequest; historyLoading.value = true; historyError.value = ''
 try { const data = await api.history(hostId, page); if (alive && id === historyRequest && props.selectedId === hostId) history.value = data }
 catch (e) { if (alive && id === historyRequest) historyError.value = e.message }
 finally { if (alive && id === historyRequest) historyLoading.value = false }
}
function detail(item) { emit('detail', item.id) }
function back() { emit('back') }
async function save(input) {
 requireAdmin()
 const item = formState.value?.item
 if (item) { await api.update(props.kind, item.id, input); await load(); if (current.value) await loadHistory(); notify('配置已保存') }
 else { const created = await api.create(props.kind, input); await load(); if (props.editable) pendingInstall = created; notify('目标已保存，等待安装探针') }
}
async function closeForm() { formState.value = null; if (pendingInstall && props.editable) { const item = pendingInstall; pendingInstall = null; await nextTick(); installing.value = item } }
async function mutate(action, message) {
 if (!props.editable || busy.value) return
 busy.value = true
 try { await action(); await load(); if (current.value) await loadHistory(1); notify(message) } catch (e) { if (alive) notify(e.message, true) } finally { busy.value = false }
}
async function check() { if (current.value) await mutate(() => api.check(current.value.id), '已提交节点测量，等待真实结果；页面每 15 秒自动刷新') }
async function askDelete(item) { requireAdmin(); if (formState.value || installing.value || deleting.value || busy.value) return; deleting.value = item; deleteError.value = ''; await nextTick(); confirmDialog.value?.showModal() }
async function remove() {
 if (!props.editable || busy.value || !deleting.value) return
 busy.value = true; deleteError.value = ''
 try { await api.remove(props.kind, deleting.value.id); if (props.kind === 'vps' && props.selectedId === deleting.value.id) back(); deleting.value = null; await load(); notify(props.kind === 'vps' ? 'VPS 及其检测历史已删除' : '节点已移除，历史结果仍然保留') }
 catch (e) { deleteError.value = e.message } finally { busy.value = false }
}
const affected = computed(() => deleting.value ? fleet.value.hosts.filter(h => h.nodeIds.includes(deleting.value.id)).length : 0)
watch(() => props.selectedId, value => { ++historyRequest; historyLoading.value = false; history.value = { records: [], total: 0, page: 1, size: 8 }; if (value) loadHistory(); nextTick(() => document.getElementById(value ? 'fleet-detail-title' : 'fleet-list-title')?.focus()) }, { immediate: true })
watch(() => props.editable, value => { if (!value) { formState.value = null; installing.value = null; deleting.value = null; pendingInstall = null } })
onMounted(() => { load(); pollTimer = setInterval(() => { clock.value = Date.now(); load(); if (props.selectedId) loadHistory(history.value.page, true) }, 15000) })
onUnmounted(() => { alive = false; clearTimeout(toastTimer); clearInterval(pollTimer); ++request; ++historyRequest; pendingInstall = null })
</script>
<template>
 <div class="fleet-view">
  <div v-if="selectedId && !current" class="empty-state"><AlertCircle :size="30" /><h3>{{loading ? '正在加载 VPS…' : '无法读取这台 VPS'}}</h3><p v-if="error" role="alert">{{error}}</p><button class="button" @click="back">返回 VPS 监控</button></div>
  <template v-else-if="!current">
   <div class="page-heading"><div><div class="eyebrow">{{kind === 'vps' ? 'VPS MONITORING' : 'TEST NODES'}}</div><h1 id="fleet-list-title" tabindex="-1">{{title}}<span>{{list.length}}</span></h1><p>{{kind === 'vps' ? '查看探针状态，从不同节点测量 VPS 延迟。' : '添加自己的测量来源，比较不同地区到 VPS 的线路。'}}</p></div><button v-if="editable" class="button primary" :disabled="loading || busy" @click="openForm()"><Plus :size="18" />{{kind === 'vps' ? '添加 VPS' : '添加节点'}}</button></div>
   <section class="status-summary" :aria-label="title + '状态汇总'"><div><span class="summary-label">{{kind === 'vps' ? 'VPS 总数' : '节点总数'}}</span><strong>{{summary.total}}<small>台</small></strong></div><div><span class="summary-label"><i class="status-dot up"></i>探针在线</span><strong>{{summary.online}}<small>台</small></strong></div><div><span class="summary-label"><i class="status-dot down"></i>探针离线</span><strong>{{summary.offline}}<small>台</small></strong></div><div><span class="summary-label"><i class="status-dot unknown"></i>等待安装</span><strong>{{summary.pending}}<small>台</small></strong></div></section>
   <section class="monitor-panel" :aria-busy="loading"><div class="panel-toolbar"><h2>{{kind === 'vps' ? '所有 VPS' : '所有测试节点'}}<span>{{kind === 'vps' ? 'Linux 探针' : '测试节点 → VPS'}}</span></h2><button class="button subtle" :disabled="loading || busy" @click="load"><RefreshCw :size="15" :class="{spin:loading}" />刷新列表</button></div>
    <div v-if="error" class="empty-state"><AlertCircle :size="30" /><h3>列表加载失败</h3><p role="alert">{{error}}</p><button class="button" @click="load">重试</button></div>
    <div v-else-if="loading && !list.length" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在加载…</p></div>
    <div v-else-if="!list.length" class="empty-state"><Server v-if="kind === 'vps'" :size="34" /><Network v-else :size="34" /><h3>{{kind === 'vps' ? '暂无 VPS' : '暂无测试节点'}}</h3><p>管理员添加并安装探针后，状态会显示在这里。</p><button v-if="editable" class="button primary" @click="openForm()"><Plus :size="16" />{{kind === 'vps' ? '添加 VPS' : '添加节点'}}</button></div>
    <div v-else class="table-scroll"><table class="monitor-table fleet-table"><thead><tr><th>{{kind === 'vps' ? 'VPS 名称 / 地址' : '节点名称 / 地区'}}</th><th>探针心跳状态</th><th>{{kind === 'vps' ? '测试节点' : '关联 VPS'}}</th><th>最近心跳</th><th class="align-right">操作</th></tr></thead><tbody><tr v-for="item in list" :key="item.id" :class="{'disabled-row':!item.enabled}"><td><div class="monitor-identity"><span :class="['service-icon',kind === 'vps' ? 'blue' : 'teal']"><Server v-if="kind === 'vps'" :size="20" /><Network v-else :size="20" /></span><div><button v-if="kind === 'vps'" class="monitor-name" @click="detail(item)">{{item.name}}<ChevronRight :size="14" /></button><b v-else class="fleet-node-name">{{item.name}}</b><span class="monitor-url">{{kind === 'vps' ? item.address : item.region || '未填写地区'}}</span></div></div></td><td><span :class="['status-badge',stateClass(item)]"><i class="status-dot"></i>{{stateLabel(item)}}</span><small v-if="!item.enabled" class="paused-label">已停用测量</small></td><td>{{kind === 'vps' ? item.nodeIds.length : fleet.hosts.filter(h => h.nodeIds.includes(item.id)).length}} 个</td><td class="muted mono">{{at(item.lastSeenAt)}}</td><td><div v-if="editable" class="row-actions"><button class="button subtle" :disabled="busy" @click="install(item)"><Terminal :size="15" />安装</button><button class="icon-button" :aria-label="'编辑 ' + item.name" title="编辑" :disabled="busy" @click="openForm(item)"><Pencil :size="16" /></button><button class="icon-button" :aria-label="(item.enabled ? '停用 ' : '启用 ') + item.name" :title="item.enabled ? '停用测量' : '启用测量'" :disabled="busy" @click="mutate(() => api.toggle(kind,item.id,!item.enabled),item.enabled ? '已停用测量，保留探针状态与历史' : '已启用测量')"><Pause v-if="item.enabled" :size="16" /><Play v-else :size="16" /></button><button class="icon-button danger-text" :aria-label="'删除 ' + item.name" title="删除" :disabled="busy" @click="askDelete(item)"><Trash2 :size="16" /></button></div><button v-else-if="kind === 'vps'" class="button subtle" @click="detail(item)">查看详情</button><span v-else class="muted">只读</span></td></tr></tbody></table></div>
    <div v-if="list.length && !error" class="table-footer"><span>共 {{list.length}} {{kind === 'vps' ? '台 VPS' : '个测试节点'}}</span><span>心跳状态与网络测量分别记录</span></div>
   </section>
   <p class="fleet-context"><CircleHelp :size="16" />{{kind === 'vps' ? '点击 VPS 名称查看各节点的延迟和丢包。' : '测试节点向关联 VPS 发起测量；心跳超过 90 秒会显示为离线。'}}</p>
  </template>
  <template v-else>
   <button class="back-button" @click="back"><ChevronLeft :size="16" />返回 VPS 监控</button>
   <div class="page-heading detail-heading"><div class="detail-identity"><span class="service-icon large blue"><Server :size="26" /></span><div><h1 id="fleet-detail-title" tabindex="-1">{{current.name}}</h1><div class="detail-url">{{current.address}}<span v-if="current.region"> · {{current.region}}</span></div></div></div><div v-if="editable" class="detail-actions"><button class="button" :disabled="busy" @click="openForm(current)"><Pencil :size="16" />编辑</button><button class="button primary" :disabled="busy || !current.enabled || state(current) === 'PENDING' || !usableNodes.length" @click="check"><LoaderCircle v-if="busy" :size="16" class="spin" /><Play v-else :size="16" />提交测量</button></div></div>
   <section class="fleet-overview"><div><span class="summary-label">VPS 探针心跳</span><span :class="['status-badge',stateClass(current)]"><i class="status-dot"></i>{{stateLabel(current)}}</span><small>最近心跳：{{at(current.lastSeenAt)}} · 超过 90 秒视为离线</small></div><div><span class="summary-label">测量方向</span><b>测试节点 → VPS</b><small>ICMP Ping · 每次 5 个包</small></div><div><span class="summary-label">可用测试节点</span><b>{{usableNodes.length}} / {{selectedNodes.length}}</b><small>{{current.enabled ? '已启用监控' : '监控已停用，结果保留'}}</small></div><button v-if="editable" class="button" :disabled="busy" @click="install(current)"><Terminal :size="16" />探针安装</button></section>
   <p v-if="!usableNodes.length" class="fleet-context"><CircleHelp :size="16" />{{!selectedNodes.length ? '尚未关联测试节点，暂无线路数据。' : '所选节点均未在线或已停用，暂无节点可以执行测量。'}}</p>
   <section class="monitor-panel"><div class="panel-toolbar"><h2>各节点测量<span>ICMP 往返延迟</span></h2><button class="button subtle" :disabled="loading || busy" @click="load"><RefreshCw :size="15" />刷新结果</button></div><p v-if="error" class="inline-error" role="alert">{{error}}</p>
    <div v-if="!selectedNodes.length" class="empty-state"><Network :size="32" /><h3>还没有测试节点</h3><button v-if="editable" class="button" @click="openForm(current)">选择测试节点</button></div>
    <div v-else class="table-scroll"><table class="monitor-table fleet-results"><thead><tr><th>测试节点</th><th>平均延迟</th><th>丢包率</th><th>测量状态</th><th>最近测量</th></tr></thead><tbody><tr v-for="node in selectedNodes" :key="node.id"><td><b>{{node.name}}</b><span class="monitor-url">{{node.region || '未填写地区'}}</span></td><td class="mono">{{rtt(latest(node)) ?? '—'}}<span v-if="rtt(latest(node)) != null" class="muted"> ms</span></td><td class="mono">{{loss(latest(node))}}</td><td><span v-if="!node.enabled" class="status-badge paused">节点已停用</span><span v-else-if="state(node) !== 'ONLINE'" class="status-badge unknown">{{state(node) === 'PENDING' ? '节点待安装' : '节点离线'}}</span><span v-else-if="!latest(node)" class="status-badge unknown">尚未测量</span><span v-else-if="stale(latest(node))" class="status-badge unknown">数据已过期</span><span v-else :class="['status-badge',latest(node).status === 'OK' ? 'up' : 'down']">{{measurementLabel(latest(node))}}</span><small v-if="latest(node) && (state(node) !== 'ONLINE' || !node.enabled || stale(latest(node)))" class="paused-label">历史结果，仅供参考</small></td><td class="mono muted">{{at(latest(node)?.checkedAt)}}<small v-if="latest(node)?.status === 'ERROR'" class="paused-label history-error">{{latest(node).error || '节点测量执行失败'}}</small></td></tr></tbody></table></div>
   </section>
   <p class="fleet-context"><CircleHelp :size="16" />Ping 无回复可能是 ICMP 被阻止；节点离线时保留旧结果，不记为 VPS 丢包。</p>
   <section class="monitor-panel history-panel" :aria-busy="historyLoading"><div class="panel-toolbar"><h2>测量历史<span>按测量时间倒序</span></h2><button class="button subtle" :disabled="historyLoading || busy" @click="loadHistory(history.page)"><RefreshCw :size="15" />刷新记录</button></div>
    <div v-if="historyError" class="empty-state"><p role="alert">{{historyError}}</p><button class="button" @click="loadHistory(history.page)">重试</button></div><div v-else-if="historyLoading" class="empty-state"><LoaderCircle :size="24" class="spin" /><p>正在加载测量记录…</p></div><div v-else-if="!history.total" class="empty-state"><h3>还没有测量记录</h3><p>等待在线测试节点完成测量后，记录会显示在这里。</p></div>
    <div v-else class="table-scroll"><table class="monitor-table fleet-results"><thead><tr><th>测量时间</th><th>测试节点</th><th>平均延迟</th><th>丢包率</th><th>测量结果</th></tr></thead><tbody><tr v-for="record in history.records" :key="record.id"><td class="mono">{{at(record.checkedAt)}}</td><td>{{record.nodeName}}<small v-if="!fleet.nodes.some(n => n.id === record.nodeId)" class="paused-label">节点已移除</small></td><td class="mono">{{rtt(record) ?? '—'}}{{rtt(record) != null ? ' ms' : ''}}</td><td class="mono">{{loss(record)}}</td><td><span :class="['status-badge',record.status === 'OK' ? 'up' : 'down']">{{measurementLabel(record)}}</span><small v-if="record.status === 'ERROR'" class="paused-label history-error">{{record.error || '节点测量执行失败'}}</small></td></tr></tbody></table></div>
    <div v-if="history.total && !historyLoading && !historyError" class="table-footer"><span>共 {{history.total}} 条记录</span><div class="pagination"><button class="icon-button" aria-label="上一页" :disabled="history.page <= 1" @click="loadHistory(history.page - 1)"><ChevronLeft :size="17" /></button><span>{{history.page}} / {{historyPages}}</span><button class="icon-button" aria-label="下一页" :disabled="history.page >= historyPages" @click="loadHistory(history.page + 1)"><ChevronRight :size="17" /></button></div></div>
   </section>
  </template>
  <FleetForm v-if="editable && formState" :kind="kind" :item="formState.item" :nodes="fleet.nodes" :on-save="save" @close="closeForm" />
  <InstallDialog v-if="editable && installing" :kind="kind" :item="installing" @close="installing = null" />
  <dialog v-if="editable && deleting" ref="confirmDialog" class="confirm-dialog" aria-labelledby="fleet-delete-title" @cancel.prevent="!busy && (deleting = null)"><div class="delete-icon"><Trash2 :size="24" /></div><h2 id="fleet-delete-title">删除{{kind === 'vps' ? '这台 VPS' : '这个测试节点'}}？</h2><p>「{{deleting.name}}」{{kind === 'vps' ? '及其测量历史将被删除，此操作无法撤销。' : '将被移除；已有测量历史保留，'+affected+' 台 VPS 的后续测量将解除此节点关联。'}}</p><p v-if="deleteError" class="inline-error" role="alert">{{deleteError}}</p><div class="dialog-footer"><button class="button" autofocus :disabled="busy" @click="deleting = null">取消</button><button class="button danger" :disabled="busy" @click="remove">{{busy ? '删除中…' : '确认删除'}}</button></div></dialog>
  <div v-if="toast" :class="['toast',{error:toast.failed}]" role="status"><AlertCircle v-if="toast.failed" :size="18" /><Check v-else :size="18" /><span>{{toast.message}}</span><button class="icon-button" aria-label="关闭提示" @click="toast = null"><X :size="15" /></button></div>
 </div>
</template>
