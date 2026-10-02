<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Terminal, X, Copy, Check, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import { fleetApi } from '../api/fleet.js'
const props = defineProps({ kind: String, item: Object })
const emit = defineEmits(['close'])
const dialog = ref(null), payload = ref(null), loading = ref(true), error = ref(''), copied = ref(false), clock = ref(Date.now())
let tick, alive = true, sequence = 0
const expired = computed(() => !payload.value || Date.parse(payload.value.expiresAt) <= clock.value)
async function load() {
 const id = ++sequence; loading.value = true; error.value = ''; copied.value = false; payload.value = null
 try {
  const data = await fleetApi.installation(props.kind, props.item.id)
  if (data?.demo !== false || typeof data.command !== 'string' || !data.command || !Number.isFinite(Date.parse(data.expiresAt))) throw new Error('安装信息无效，请检查后端服务')
  if (alive && id === sequence) payload.value = data
 } catch (e) { if (alive && id === sequence) error.value = e.message }
 finally { if (alive && id === sequence) loading.value = false }
}
async function copy() {
 if (expired.value || !payload.value) return
 try { await navigator.clipboard.writeText(payload.value.command); if (alive) copied.value = true }
 catch { if (alive) error.value = '复制失败，请手动选中下方命令复制。' }
}
function close() { payload.value = null; ++sequence; emit('close') }
onMounted(() => { dialog.value.showModal(); load(); tick = setInterval(() => { clock.value = Date.now() }, 1000) })
onUnmounted(() => { alive = false; payload.value = null; ++sequence; clearInterval(tick) })
</script>
<template>
 <dialog ref="dialog" class="form-dialog fleet-dialog installation-dialog" aria-labelledby="install-title" @cancel.prevent="close">
  <div class="dialog-heading"><div class="dialog-icon"><Terminal :size="22" /></div><button class="icon-button" aria-label="关闭安装引导" @click="close"><X :size="20" /></button></div>
  <h2 id="install-title">{{kind === 'vps' ? '安装 VPS 探针' : '安装测试节点'}}</h2><p class="dialog-description">{{item.name}} · Ubuntu / Debian</p>
  <div class="install-steps"><span class="step-complete">1 保存目标</span><span>2 SSH 安装探针</span><span>3 等待真实心跳</span></div>
  <div v-if="loading" class="install-loading"><LoaderCircle :size="22" class="spin" />正在生成一次性安装信息…</div>
  <template v-else-if="payload">
   <p class="installation-notice">通过 SSH 登录对应 Linux 服务器，粘贴执行下方命令。主控须可通过 HTTPS 访问；请勿在其他服务器执行或公开分享命令。</p>
   <pre class="install-command" tabindex="0" aria-label="安装命令"><code>{{payload.command}}</code></pre>
   <div class="install-command-actions"><button class="button" :disabled="expired" @click="copy"><Check v-if="copied" :size="16" /><Copy v-else :size="16" />{{copied ? '已复制' : '复制安装命令'}}</button><span v-if="expired" class="field-error">安装令牌已过期，请重新生成</span><small v-else class="field-hint">10 分钟内单次使用 · 有效至 {{new Date(payload.expiresAt).toLocaleString('zh-CN')}}</small></div>
   <button v-if="expired" class="button" @click="load"><RefreshCw :size="16" />重新生成</button>
   <p class="install-role">{{kind === 'vps' ? 'VPS 探针上报真实心跳；线路延迟由关联的测试节点测量。' : '测试节点领取主控任务，向 VPS 发起 Ping 并上报真实结果。'}}安装完成后，列表会随心跳自动刷新。</p>
  </template>
  <p v-if="error" class="inline-error" role="alert">{{error}}</p><button v-if="!payload && !loading" class="button" @click="load">重试</button>
  <div class="dialog-footer"><button class="button" @click="close">关闭</button></div>
 </dialog>
</template>
