<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { Server, Network, Plus, RefreshCw, LoaderCircle, AlertCircle, ChevronLeft, ChevronRight, Pencil, Pause, Play, Trash2, Terminal, CircleHelp, Check, X } from 'lucide-vue-next'
import { fleetApi as api } from '../api/fleet.js'
import { agentState, resultIsStale } from '../api/time.js'
import { targetConfigured, canMeasureHost, formatEndpoint, latestMeasurement, failureRate as loss, measurementRtt as rawRtt, measurementLabel, failureLabel, directionLabel, measurementProtocol } from '../api/measurements.js'
import FleetForm from './FleetForm.vue'
import InstallDialog from './InstallDialog.vue'
const props = defineProps({ kind: String, editable: Boolean, selectedId: Number })
const emit = defineEmits(['detail', 'back'])
const fleet = ref({ hosts: [], nodes: [], results: [] }), loading = ref(true), error = ref(''), formState = ref(null), installing = ref(null), deleting = ref(null), confirmDialog = ref(null), busy = ref(false), deleteError = ref(''), toast = ref(null)
const history = ref({ records: [], total: 0, page: 1, size: 8 }), historyLoading = ref(false), historyError = ref(''), clock = ref(Date.now())
let toastTimer, pollTimer, pendingInstall, alive = true, request = 0, historyRequest = 0
const title = computed(() => props.kind === 'vps' ? 'VPS 监控' : '测速目标')
const list = computed(() => props.kind === 'vps' ? fleet.value.hosts : fleet.value.nodes)
const current = computed(() => props.kind === 'vps' ? fleet.value.hosts.find(h => h.id === props.selectedId) : null)
const state = item => agentState(item, clock.value)
const summary = computed(() => props.kind === 'vps' ? { total: list.value.length, online: list.value.filter(x => state(x) === 'ONLINE').length, offline: list.value.filter(x => state(x) === 'OFFLINE').length, pending: list.value.filter(x => state(x) === 'PENDING').length } : { total: list.value.length, online: list.value.filter(x => x.enabled && targetConfigured(x)).length, offline: list.value.filter(x => !x.enabled && targetConfigured(x)).length, pending: list.value.filter(x => !targetConfigured(x)).length })
const selectedNodes = computed(() => fleet.value.nodes.filter(n => current.value?.nodeIds.includes(n.id)))
const usableNodes = computed(() => selectedNodes.value.filter(n => n.enabled && targetConfigured(n)))
const canMeasure = computed(() => canMeasureHost(current.value, selectedNodes.value, clock.value))
const historyPages = computed(() => Math.max(1, Math.ceil(history.value.total / history.value.size)))
const stateLabel = item => state(item) === 'ONLINE' ? '在线' : state(item) === 'OFFLINE' ? '离线' : '待安装'
const stateClass = item => state(item) === 'ONLINE' ? 'up' : state(item) === 'OFFLINE' ? 'down' : 'unknown'
const at = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
const rtt = result => { const value = rawRtt(result); return value === null ? null : Number(value.toFixed(2)) }
const latest = node => latestMeasurement(fleet.value.results, current.value?.id, node)
const stale = result => resultIsStale(result, clock.value)
function notify(message, failed = false) { clearTimeout(toastTimer); toast.value = { message, failed }; toastTimer = setTimeout(() => { toast.value = null }, 4500) }
function requireAdmin() { if (!props.editable) throw new Error('请先登录管理员后台') }
function openForm(item = null) { requireAdmin(); if (!busy.value && !formState.value && !installing.value && !deleting.value) formState.value = { item } }
function install(item, mode = 'fresh') { requireAdmin(); if (props.kind === 'vps' && !busy.value && !formState.value && !installing.value && !deleting.value) installing.value = { item, mode } }
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
 else { const created = await api.create(props.kind, input); await load(); if (props.editable && props.kind === 'vps') pendingInstall = created; notify(props.kind === 'vps' ? 'VPS 已保存，等待安装探针' : '测速目标已保存；无需安装探针') }
}
async function closeForm() { formState.value = null; if (pendingInstall && props.editable) { const item = pendingInstall; pendingInstall = null; await nextTick(); installing.value = { item, mode: 'fresh' } } }
async function mutate(action, message) {
 if (!props.editable || busy.value) return
 busy.value = true
 try { await action(); await load(); if (current.value) await loadHistory(1); notify(message) } catch (e) { if (alive) notify(e.message, true) } finally { busy.value = false }
}
async function check() { if (canMeasure.value) await mutate(() => api.check(current.value.id), '已提交 VPS 出站测量，等待真实结果；页面每 15 秒自动刷新') }
async function askDelete(item) { requireAdmin(); if (formState.value || installing.value || deleting.value || busy.value) return; deleting.value = item; deleteError.value = ''; await nextTick(); confirmDialog.value?.showModal() }
async function remove() {
 if (!props.editable || busy.value || !deleting.value) return
 busy.value = true; deleteError.value = ''
 try { await api.remove(props.kind, deleting.value.id); if (props.kind === 'vps' && props.selectedId === deleting.value.id) back(); deleting.value = null; await load(); notify(props.kind === 'vps' ? 'VPS 及其检测历史已删除' : '测速目标已移除，历史结果仍然保留') }
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
   <div class="page-heading list-heading"><div><h1 id="fleet-list-title" tabindex="-1">{{title}}<span>{{list.length}}</span></h1><p>{{kind === 'vps' ? '每台 VPS 向外部测速目标测量出站延迟。' : '外部 DNS 主机名或 IP；选择 ICMP Ping 或 TCP 连接。'}}</p></div><button v-if="editable" class="button primary" :disabled="loading || busy" @click="openForm()"><Plus :size="18" />{{kind === 'vps' ? '添加 VPS' : '添加目标'}}</button></div>
   <section class="status-summary" :aria-label="title + '状态汇总'">
    <span class="summary-item"><strong>{{summary.total}}</strong><span class="summary-label">{{kind === 'vps' ? '台 VPS' : '个目标'}}</span></span>
    <span class="summary-item"><i class="status-dot up"></i><strong>{{summary.online}}</strong><span class="summary-label">{{kind === 'vps' ? '探针在线' : '已启用'}}</span></span>
    <span class="summary-item"><i class="status-dot down"></i><strong>{{summary.offline}}</strong><span class="summary-label">{{kind === 'vps' ? '探针离线' : '已停用'}}</span></span>
    <span class="summary-item"><i class="status-dot unknown"></i><strong>{{summary.pending}}</strong><span class="summary-label">{{kind === 'vps' ? '待安装' : '待补地址'}}</span></span>
   </section>
   <section class="monitor-panel" :aria-busy="loading"><div class="panel-toolbar"><h2>{{kind === 'vps' ? '所有 VPS' : '所有测速目标'}}<span>{{kind === 'vps' ? 'Linux VPS 探针' : '无需安装探针'}}</span></h2><button class="button subtle" :disabled="loading || busy" @click="load"><RefreshCw :size="15" :class="{spin:loading}" />刷新列表</button></div>
    <div v-if="error" class="empty-state"><AlertCircle :size="30" /><h3>列表加载失败</h3><p role="alert">{{error}}</p><button class="button" @click="load">重试</button></div>
    <div v-else-if="loading && !list.length" class="empty-state"><LoaderCircle :size="28" class="spin" /><p>正在加载…</p></div>
    <div v-else-if="!list.length" class="empty-state"><Server v-if="kind === 'vps'" :size="34" /><Network v-else :size="34" /><h3>{{kind === 'vps' ? '暂无 VPS' : '暂无测速目标'}}</h3><p>{{kind === 'vps' ? '管理员添加 VPS 并安装探针后，真实心跳会显示在这里。' : '管理员配置外部地址并关联 VPS 后，可查看出站测量。'}}</p><button v-if="editable" class="button primary" @click="openForm()"><Plus :size="16" />{{kind === 'vps' ? '添加 VPS' : '添加目标'}}</button></div>
    <div v-else class="table-scroll"><table class="monitor-table fleet-table responsive-table"><thead><tr><th>{{kind === 'vps' ? 'VPS 名称 / 地址' : '目标名称 / 地址'}}</th><th>{{kind === 'vps' ? '探针心跳' : '协议'}}</th><th>{{kind === 'vps' ? '测速目标' : '关联 VPS'}}</th><th>{{kind === 'vps' ? '最近心跳' : '配置状态'}}</th><th class="align-right">操作</th></tr></thead><tbody>
     <tr v-for="item in list" :key="item.id" :class="{'disabled-row':!item.enabled}">
      <td :data-label="kind === 'vps' ? 'VPS 名称 / 地址' : '目标名称 / 地址'"><div class="monitor-identity"><span class="service-icon"><Server v-if="kind === 'vps'" :size="20" /><Network v-else :size="20" /></span><div><button v-if="kind === 'vps'" class="monitor-name" @click="detail(item)">{{item.name}}<ChevronRight :size="14" /></button><b v-else class="fleet-node-name">{{item.name}}</b><span class="monitor-url">{{kind === 'vps' ? item.address : targetConfigured(item) ? formatEndpoint(item.address,item.protocol,item.port) : '请补齐目标地址'}}</span><small v-if="item.region" class="paused-label">{{item.region}}</small></div></div></td>
      <td :data-label="kind === 'vps' ? '探针心跳' : '协议'"><template v-if="kind === 'vps'"><span :class="['status-badge',stateClass(item)]"><i class="status-dot"></i>{{stateLabel(item)}}</span><small v-if="item.agentUpdateRequired" class="field-error paused-label">探针需升级</small><small v-if="!item.enabled" class="paused-label">已停用测量</small></template><span v-else class="status-badge unknown">{{item.protocol || '待配置'}}</span></td>
      <td :data-label="kind === 'vps' ? '测速目标' : '关联 VPS'">{{kind === 'vps' ? item.nodeIds.length : fleet.hosts.filter(h => h.nodeIds.includes(item.id)).length}} {{kind === 'vps' ? '个' : '台'}}</td>
      <td data-label="最近心跳" v-if="kind === 'vps'" class="muted mono">{{at(item.lastSeenAt)}}</td><td data-label="配置状态" v-else><span :class="['status-badge',!targetConfigured(item) ? 'unknown' : item.enabled ? 'up' : 'paused']">{{!targetConfigured(item) ? '待补地址 · 已停用' : item.enabled ? '已启用' : '已停用'}}</span><small v-if="!targetConfigured(item)" class="paused-label">旧版目标保留，需管理员配置地址</small></td>
      <td data-label="操作"><div v-if="editable" class="row-actions"><button v-if="kind === 'vps'" class="button subtle" :disabled="busy" @click="install(item,item.agentUpdateRequired ? 'upgrade' : 'fresh')"><Terminal :size="15" />{{item.agentUpdateRequired ? '升级探针' : '安装 / 升级'}}</button><button class="icon-button" :aria-label="'编辑 ' + item.name" title="编辑" :disabled="busy" @click="openForm(item)"><Pencil :size="16" /></button><button class="icon-button" :aria-label="(item.enabled ? '停用 ' : '启用 ') + item.name" :title="item.enabled ? '停用测量' : '启用测量'" :disabled="busy || (kind !== 'vps' && !targetConfigured(item) && !item.enabled)" @click="mutate(() => api.toggle(kind,item.id,!item.enabled),item.enabled ? '已停用，历史结果保留' : '已启用')"><Pause v-if="item.enabled" :size="16" /><Play v-else :size="16" /></button><button class="icon-button danger-text" :aria-label="'删除 ' + item.name" title="删除" :disabled="busy" @click="askDelete(item)"><Trash2 :size="16" /></button></div><button v-else-if="kind === 'vps'" class="button subtle" @click="detail(item)">查看详情</button><span v-else class="muted">只读</span></td>
     </tr>
    </tbody></table></div>
    <div v-if="list.length && !error" class="table-footer"><span>共 {{list.length}} {{kind === 'vps' ? '台 VPS' : '个测速目标'}}</span><span>{{kind === 'vps' ? '心跳状态与测量结果分别记录' : '配置不代表可达；实际可达性由 VPS 测量'}}</span></div>
   </section>
   <p class="fleet-context"><CircleHelp :size="16" />{{kind === 'vps' ? '点击 VPS 名称查看出站延迟。VPS 心跳超过 90 秒显示离线。' : '测速目标无需安装或心跳。公网地址由 VPS 端解析并测量，不会自动将旧目标名称当作地址。'}}</p>
  </template>
  <template v-else>
   <button class="back-button" @click="back"><ChevronLeft :size="16" />返回 VPS 监控</button>
   <div class="page-heading detail-heading"><div class="detail-identity"><span class="service-icon large"><Server :size="26" /></span><div><h1 id="fleet-detail-title" tabindex="-1">{{current.name}}</h1><div class="detail-url">{{current.address}}<span v-if="current.region"> · {{current.region}}</span></div></div></div><div v-if="editable" class="detail-actions"><button class="button" :disabled="busy" @click="openForm(current)"><Pencil :size="16" />编辑</button><button class="button primary" :disabled="busy || !canMeasure" @click="check"><LoaderCircle v-if="busy" :size="16" class="spin" /><Play v-else :size="16" />提交出站测量</button></div></div>
   <section class="fleet-overview"><div><span class="summary-label">VPS 探针心跳</span><span :class="['status-badge',stateClass(current)]"><i class="status-dot"></i>{{stateLabel(current)}}</span><small>最近心跳：{{at(current.lastSeenAt)}} · 超过 90 秒视为离线</small></div><div><span class="summary-label">测量方向</span><b>VPS → 测速目标</b><small>ICMP：5 个包 / TCP：5 次连接</small></div><div><span class="summary-label">已启用的配置目标</span><b>{{usableNodes.length}} / {{selectedNodes.length}}</b><small>{{current.enabled ? '已启用监控' : '已停用，结果保留'}}</small></div><button v-if="editable" class="button" :disabled="busy" @click="install(current,current.agentUpdateRequired ? 'upgrade' : 'fresh')"><Terminal :size="16" />{{current.agentUpdateRequired ? '升级 VPS 探针' : '探针安装 / 升级'}}</button></section>
   <p v-if="current.agentUpdateRequired" class="installation-notice" role="status">这台 VPS 的旧探针仅会上报心跳，需要升级后才能测量 VPS → 测速目标。{{editable ? '请在这台 VPS 的 SSH 终端执行上方升级命令。' : '请联系管理员升级此 VPS 探针。'}}</p>
   <p v-else-if="state(current) !== 'ONLINE'" class="fleet-context"><CircleHelp :size="16" />{{state(current) === 'PENDING' ? 'VPS 尚未安装探针。' : 'VPS 探针已离线，当前不会执行测量。'}}旧结果保留，不会将离线视为目标丢包。</p>
   <p v-if="!usableNodes.length" class="fleet-context"><CircleHelp :size="16" />{{!selectedNodes.length ? '尚未关联测速目标，暂无出站线路数据。' : '关联目标已停用或待补地址，暂无可执行的测速目标。'}}</p>
   <section class="monitor-panel"><div class="panel-toolbar"><h2>出站测量<span>ICMP 往返 / TCP 连接延迟</span></h2><button class="button subtle" :disabled="loading || busy" @click="load"><RefreshCw :size="15" />刷新结果</button></div><p v-if="error" class="inline-error" role="alert">{{error}}；下方保留最后一次读取的数据。</p>
    <div v-if="!selectedNodes.length" class="empty-state"><Network :size="32" /><h3>还没有测速目标</h3><button v-if="editable" class="button" @click="openForm(current)">选择测速目标</button></div>
    <div v-else class="table-scroll"><table class="monitor-table fleet-results responsive-table"><thead><tr><th>测速目标 / 协议</th><th>平均延迟</th><th>失败指标</th><th>测量状态</th><th>最近测量</th></tr></thead><tbody>
     <tr v-for="node in selectedNodes" :key="node.id"><td data-label="测速目标 / 协议"><b>{{node.name}}</b><span class="monitor-url">{{targetConfigured(node) ? node.protocol + ' · ' + formatEndpoint(node.address,node.protocol,node.port) : '待补目标地址'}}</span></td><td data-label="平均延迟" class="mono">{{rtt(latest(node)) ?? '—'}}<span v-if="rtt(latest(node)) != null" class="muted"> ms</span></td><td data-label="失败指标" class="mono">{{loss(latest(node))}}<small class="paused-label">{{node.protocol === 'TCP' ? '连接失败率' : '丢包率'}}</small></td><td data-label="测量状态"><span v-if="!targetConfigured(node)" class="status-badge unknown">待补地址</span><span v-else-if="!node.enabled" class="status-badge paused">目标已停用</span><span v-else-if="!latest(node)" class="status-badge unknown">尚未测量</span><span v-else-if="stale(latest(node))" class="status-badge unknown">数据已过期</span><span v-else :class="['status-badge',latest(node).status === 'OK' ? 'up' : 'down']">{{measurementLabel(latest(node))}}</span><small v-if="latest(node) && (state(current) !== 'ONLINE' || !current.enabled || !node.enabled || stale(latest(node)))" class="paused-label">历史结果，仅供参考</small></td><td data-label="最近测量" class="mono muted">{{at(latest(node)?.checkedAt)}}<small v-if="latest(node)?.error" class="paused-label history-error">{{latest(node).error}}</small></td></tr>
    </tbody></table></div>
   </section>
   <p class="fleet-context"><CircleHelp :size="16" />ICMP 丢包率与 TCP 连接失败率分别计算。TCP 连接被拒绝或超时不代表 ICMP 丢包；刷新仅读取已有结果。</p>
   <section class="monitor-panel history-panel" :aria-busy="historyLoading"><div class="panel-toolbar"><h2>测量历史<span>保留测量时的方向和目标快照</span></h2><button class="button subtle" :disabled="historyLoading || busy" @click="loadHistory(history.page)"><RefreshCw :size="15" />刷新记录</button></div>
    <div v-if="historyError" class="empty-state"><p role="alert">{{historyError}}</p><button class="button" @click="loadHistory(history.page)">重试</button></div><div v-else-if="historyLoading" class="empty-state"><LoaderCircle :size="24" class="spin" /><p>正在加载测量记录…</p></div><div v-else-if="!history.total" class="empty-state"><h3>还没有测量记录</h3><p>等待在线 VPS 探针完成真实出站测量后，记录会显示在这里。</p></div>
    <div v-else class="table-scroll"><table class="monitor-table fleet-results responsive-table"><thead><tr><th>测量时间 / 方向</th><th>目标快照 / 协议</th><th>平均延迟</th><th>失败指标</th><th>测量结果</th></tr></thead><tbody>
     <tr v-for="record in history.records" :key="record.id"><td data-label="测量时间 / 方向" class="mono">{{at(record.checkedAt)}}<small class="paused-label">{{directionLabel(record)}}</small></td><td data-label="目标快照 / 协议"><b>{{record.nodeName}}</b><span class="monitor-url">{{measurementProtocol(record) || '未记录协议'}} · {{formatEndpoint(record.targetAddress,measurementProtocol(record),record.targetPort)}}</span><small v-if="!fleet.nodes.some(n => n.id === record.nodeId)" class="paused-label">{{record.direction === 'NODE_TO_VPS' ? '旧节点已移除' : '目标已移除'}}</small></td><td data-label="平均延迟" class="mono">{{rtt(record) ?? '—'}}{{rtt(record) != null ? ' ms' : ''}}</td><td data-label="失败指标" class="mono">{{loss(record)}}<small class="paused-label">{{failureLabel(record)}}</small></td><td data-label="测量结果"><span :class="['status-badge',record.status === 'OK' ? 'up' : 'down']">{{measurementLabel(record)}}</span><small v-if="record.error" class="paused-label history-error">{{record.error}}</small></td></tr>
    </tbody></table></div>
    <div v-if="history.total && !historyLoading && !historyError" class="table-footer"><span>共 {{history.total}} 条记录</span><div class="pagination"><button class="icon-button" aria-label="上一页" :disabled="history.page <= 1" @click="loadHistory(history.page - 1)"><ChevronLeft :size="17" /></button><span>{{history.page}} / {{historyPages}}</span><button class="icon-button" aria-label="下一页" :disabled="history.page >= historyPages" @click="loadHistory(history.page + 1)"><ChevronRight :size="17" /></button></div></div>
   </section>
  </template>
  <FleetForm v-if="editable && formState" :kind="kind" :item="formState.item" :nodes="fleet.nodes" :on-save="save" @close="closeForm" />
  <InstallDialog v-if="editable && kind === 'vps' && installing" kind="vps" :item="installing.item" :initial-mode="installing.mode" @close="installing = null" />
  <dialog v-if="editable && deleting" ref="confirmDialog" class="confirm-dialog" aria-labelledby="fleet-delete-title" @cancel.prevent="!busy && (deleting = null)"><div class="delete-icon"><Trash2 :size="24" /></div><h2 id="fleet-delete-title">删除{{kind === 'vps' ? '这台 VPS' : '这个测速目标'}}？</h2><p>「{{deleting.name}}」{{kind === 'vps' ? '及其测量历史将被删除，此操作无法撤销。' : '将被移除；已有测量历史保留，'+affected+' 台 VPS 的后续测量将解除此目标关联。'}}</p><p v-if="deleteError" class="inline-error" role="alert">{{deleteError}}</p><div class="dialog-footer"><button class="button" autofocus :disabled="busy" @click="deleting = null">取消</button><button class="button danger" :disabled="busy" @click="remove">{{busy ? '删除中…' : '确认删除'}}</button></div></dialog>
  <div v-if="toast" :class="['toast',{error:toast.failed}]" role="status"><AlertCircle v-if="toast.failed" :size="18" /><Check v-else :size="18" /><span>{{toast.message}}</span><button class="icon-button" aria-label="关闭提示" @click="toast = null"><X :size="15" /></button></div>
 </div>
</template>
