<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Terminal, X, Copy, Check, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import { fleetApi } from '../api/fleet.js'
const props = defineProps({ kind: String, item: Object, initialMode: { type: String, default: 'fresh' } })
const emit = defineEmits(['close'])
const dialog = ref(null), payload = ref(null), loading = ref(true), error = ref(''), copied = ref(false), clock = ref(Date.now()), mode = ref(props.initialMode === 'upgrade' ? 'upgrade' : 'fresh')
let tick, alive = true, sequence = 0
const expired = computed(() => !payload.value || Date.parse(payload.value.expiresAt) <= clock.value)
const command = computed(() => mode.value === 'upgrade' ? payload.value?.upgradeCommand : payload.value?.command)
const canUpgrade = computed(() => props.item.agentState !== 'PENDING')
function choose(value) { if (value === 'upgrade' && !canUpgrade.value) return; mode.value = value; copied.value = false }
async function load() {
 const id = ++sequence; loading.value = true; error.value = ''; copied.value = false; payload.value = null
 try {
  const data = await fleetApi.installation(props.kind, props.item.id)
  if (data?.demo !== false || typeof data.command !== 'string' || !data.command || typeof data.upgradeCommand !== 'string' || !data.upgradeCommand || !Number.isFinite(Date.parse(data.expiresAt))) throw new Error('安装信息无效，请检查后端服务')
  if (alive && id === sequence) payload.value = data
 } catch (e) { if (alive && id === sequence) error.value = e.message }
 finally { if (alive && id === sequence) loading.value = false }
}
async function copy() {
 if (expired.value || !command.value) return
 try { await navigator.clipboard.writeText(command.value); if (alive) copied.value = true }
 catch { if (alive) error.value = '复制失败，请手动选中下方命令复制。' }
}
function close() { payload.value = null; ++sequence; emit('close') }
onMounted(() => { if (!canUpgrade.value) mode.value = 'fresh'; dialog.value.showModal(); load(); tick = setInterval(() => { clock.value = Date.now() }, 1000) })
onUnmounted(() => { alive = false; payload.value = null; ++sequence; clearInterval(tick) })
</script>
<template>
 <dialog ref="dialog" class="form-dialog fleet-dialog installation-dialog" aria-labelledby="install-title" @cancel.prevent="close">
  <div class="dialog-heading"><div class="dialog-icon"><Terminal :size="22" /></div><button class="icon-button" aria-label="关闭安装引导" @click="close"><X :size="20" /></button></div>
  <h2 id="install-title">{{mode === 'upgrade' ? '升级 VPS 探针' : '安装 VPS 探针'}}</h2><p class="dialog-description">{{item.name}} · Ubuntu / Debian</p>
  <div class="install-mode" role="group" aria-label="探针操作"><button class="button" :class="{primary:mode === 'fresh'}" :aria-pressed="mode === 'fresh'" @click="choose('fresh')">全新安装 / 重新注册</button><button class="button" :class="{primary:mode === 'upgrade'}" :aria-pressed="mode === 'upgrade'" :disabled="!canUpgrade" @click="choose('upgrade')">升级已有探针</button></div>
  <div v-if="loading" class="install-loading"><LoaderCircle :size="22" class="spin" />正在准备安装与升级命令…</div>
  <template v-else-if="payload">
   <p class="installation-notice">通过 SSH 登录「{{item.name}}」这台 VPS，粘贴执行下方命令。主控须可通过 HTTPS 访问；测速目标无需安装探针。</p>
   <p class="install-role">{{mode === 'upgrade' ? '升级仅用于已安装本项目 VPS 探针的原服务器，保留现有登记凭据和配置；不会把旧测试节点探针转为 VPS。' : '全新安装使用 10 分钟内有效的单次令牌。重新注册会更换该 VPS 的探针凭据，旧凭据随之失效。'}}</p>
   <pre class="install-command" tabindex="0" :aria-label="mode === 'upgrade' ? '升级命令' : '安装命令'"><code>{{command}}</code></pre>
   <div class="install-command-actions"><button class="button" :disabled="expired || !command" @click="copy"><Check v-if="copied" :size="16" /><Copy v-else :size="16" />{{copied ? '已复制' : mode === 'upgrade' ? '复制升级命令' : '复制安装命令'}}</button><span v-if="expired" class="field-error">安装信息已过期，请重新生成</span><small v-else class="field-hint">本次信息有效至 {{new Date(payload.expiresAt).toLocaleString('zh-CN')}}</small></div>
   <button v-if="expired" class="button" @click="load"><RefreshCw :size="16" />重新生成</button>
   <p class="install-role">VPS 探针上报心跳，并向关联的外部测速目标执行 ICMP 或 TCP 测量。完成后页面会随真实心跳与结果自动刷新。</p>
  </template>
  <p v-if="error" class="inline-error" role="alert">{{error}}</p><button v-if="!payload && !loading" class="button" @click="load">重试</button>
  <div class="dialog-footer"><button class="button" @click="close">关闭</button></div>
 </dialog>
</template>