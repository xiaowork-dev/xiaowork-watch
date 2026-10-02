<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Terminal, X, Copy, Check, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import { fleetApi } from '../api/fleet.js'
import { isDemo } from '../api/monitors.js'
const props = defineProps({ kind: String, item: Object, onRegistered: Function })
const emit = defineEmits(['close'])
const dialog = ref(null), payload = ref(null), loading = ref(true), error = ref(''), copied = ref(false), registering = ref(false), registered = ref(false), clock = ref(Date.now())
let tick, alive = true
const expired = computed(() => payload.value?.expiresAt && new Date(payload.value.expiresAt).getTime() <= clock.value)
async function load() {
 loading.value = true; error.value = ''; copied.value = false
 try { const data = await fleetApi.installation(props.kind, props.item.id); if (alive) payload.value = data }
 catch (e) { if (alive) error.value = e.message }
 finally { if (alive) loading.value = false }
}
async function copy() {
 if (expired.value) return
 try { await navigator.clipboard.writeText(payload.value.command); copied.value = true } catch { error.value = '复制失败，请手动选中下方模板复制。' }
}
async function register() {
 if (registering.value || !isDemo) return
 registering.value = true; error.value = ''
 try { await fleetApi.registerDemo(props.kind, props.item.id); await props.onRegistered(); registered.value = true }
 catch (e) { error.value = e.message } finally { registering.value = false }
}
function close() { if (!registering.value) emit('close') }
onMounted(() => { dialog.value.showModal(); load(); tick = setInterval(() => { clock.value = Date.now() }, 1000) })
onUnmounted(() => { alive = false; clearInterval(tick) })
</script>
<template>
 <dialog ref="dialog" class="form-dialog fleet-dialog installation-dialog" aria-labelledby="install-title" @cancel.prevent="close">
  <div class="dialog-heading"><div class="dialog-icon"><Terminal :size="22" /></div><button class="icon-button" aria-label="关闭安装引导" :disabled="registering" @click="close"><X :size="20" /></button></div>
  <h2 id="install-title">{{kind === 'vps' ? '安装 VPS 探针' : '安装测试节点'}}</h2><p class="dialog-description">{{item.name}} · Ubuntu / Debian</p>
  <div class="install-steps"><span class="step-complete">1 保存目标</span><span>2 安装探针</span><span :class="{'step-complete':registered}">3 注册上线</span></div>
  <div v-if="loading" class="install-loading"><LoaderCircle :size="22" class="spin" />正在准备安装信息…</div>
  <template v-else-if="payload">
   <p v-if="payload.demo" class="prototype-explanation">安装流程示例：当前尚无可下载的探针或主控注册服务。下面所有命令均已注释，复制不会安装程序。</p>
   <p v-else class="prototype-explanation">在对应 Linux 服务器执行下方命令。注册码仅供本次安装使用，请勿公开分享。</p>
   <pre class="install-command" tabindex="0" aria-label="安装命令"><code>{{payload.command}}</code></pre>
   <div class="install-command-actions"><button class="button" :disabled="expired || !payload.command" @click="copy"><Check v-if="copied" :size="16" /><Copy v-else :size="16" />{{copied ? '已复制' : payload.demo ? '复制命令模板' : '复制安装命令'}}</button><span v-if="expired" class="field-error">注册码已过期</span><small v-else-if="payload.expiresAt" class="field-hint">有效至 {{new Date(payload.expiresAt).toLocaleString('zh-CN')}}</small></div>
   <button v-if="expired" class="button" @click="load"><RefreshCw :size="16" />重新生成</button>
   <p class="install-role">{{kind === 'vps' ? 'VPS 探针向主控上报心跳；延迟由你选择的测试节点测量。' : '测试节点从主控领取任务，向 VPS 发起 Ping 并上报结果。'}}</p>
   <p v-if="registered" class="registration-success" role="status"><Check :size="17" />演示注册完成，示例状态已更新为在线。</p>
  </template>
  <p v-if="error" class="inline-error" role="alert">{{error}}</p><button v-if="!payload && !loading" class="button" @click="load">重试</button>
  <div class="dialog-footer"><button class="button" :disabled="registering" @click="close">关闭</button><button v-if="isDemo && payload" class="button primary" :disabled="registering || registered" @click="register"><LoaderCircle v-if="registering" :size="16" class="spin" />{{registered ? '已演示上线' : '演示注册上线'}}</button></div>
 </dialog>
</template>
