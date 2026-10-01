<script setup>
import { ref, reactive, onMounted } from 'vue'
import { X, Globe2, LoaderCircle } from 'lucide-vue-next'
import { validateMonitor } from '../api/model.js'
const props = defineProps({ monitor: Object, onSave: Function }), emit = defineEmits(['close'])
const dialog = ref(null), firstInput = ref(null), saving = ref(false), errors = ref({}), serverError = ref('')
const form = reactive({ name: props.monitor?.name || '', url: props.monitor?.url || '', method: props.monitor?.method || 'GET', intervalSeconds: props.monitor?.intervalSeconds || 60, timeoutMs: props.monitor?.timeoutMs || 5000, enabled: props.monitor?.enabled ?? true })
onMounted(() => { dialog.value.showModal(); firstInput.value.focus() })
async function submit() {
 if (saving.value) return
 const result = validateMonitor(form); errors.value = result.errors; serverError.value = ''
 if (Object.keys(result.errors).length) return
 saving.value = true
 try { await props.onSave(result.value); emit('close') } catch (error) { serverError.value = error.message } finally { saving.value = false }
}
function close() { if (!saving.value) emit('close') }
</script>
<template>
 <dialog ref="dialog" class="form-dialog" aria-labelledby="form-title" @cancel.prevent="close">
  <div class="dialog-heading"><div class="dialog-icon"><Globe2 :size="21" /></div><button class="icon-button" aria-label="关闭表单" :disabled="saving" @click="close"><X :size="20" /></button></div>
  <h2 id="form-title">{{monitor ? '编辑监控' : '新增监控'}}</h2><p class="dialog-description">设置目标地址和检测频率。</p>
  <form novalidate @submit.prevent="submit">
   <div class="form-fields">
    <div class="field"><label for="monitor-name">监控名称<span>*</span></label><input id="monitor-name" ref="firstInput" v-model="form.name" placeholder="例如：个人博客" maxlength="100" required :aria-invalid="Boolean(errors.name)" aria-describedby="name-error" :disabled="saving" /><small v-if="errors.name" id="name-error" class="field-error">{{errors.name}}</small></div>
    <div class="field"><label for="monitor-url">目标 URL<span>*</span></label><input id="monitor-url" v-model="form.url" type="url" placeholder="https://example.com" maxlength="1024" required :aria-invalid="Boolean(errors.url)" aria-describedby="url-hint" :disabled="saving" /><small id="url-hint" :class="errors.url ? 'field-error' : 'field-hint'">{{errors.url || '支持 HTTP / HTTPS 地址'}}</small></div>
    <div class="field"><label for="monitor-method">请求方法</label><select id="monitor-method" v-model="form.method" :disabled="saving"><option>GET</option><option>HEAD</option></select><small class="field-hint">HTTP 200–399 为检测成功</small></div>
    <div class="form-row"><div class="field"><label for="monitor-interval">检测间隔</label><div class="input-unit"><input id="monitor-interval" v-model.number="form.intervalSeconds" type="number" min="1" step="1" :aria-invalid="Boolean(errors.intervalSeconds)" aria-describedby="interval-error" :disabled="saving" /><span>秒</span></div><small v-if="errors.intervalSeconds" id="interval-error" class="field-error">{{errors.intervalSeconds}}</small></div><div class="field"><label for="monitor-timeout">超时时间</label><div class="input-unit"><input id="monitor-timeout" v-model.number="form.timeoutMs" type="number" min="1" step="1" :aria-invalid="Boolean(errors.timeoutMs)" aria-describedby="timeout-error" :disabled="saving" /><span>毫秒</span></div><small v-if="errors.timeoutMs" id="timeout-error" class="field-error">{{errors.timeoutMs}}</small></div></div>
    <label class="enable-field"><span><b>启用监控</b><small>保存后允许执行检测</small></span><input v-model="form.enabled" type="checkbox" role="switch" aria-label="启用监控" :disabled="saving" /><span class="switch-track" aria-hidden="true"></span></label>
    <p v-if="serverError" class="inline-error" role="alert">{{serverError}}</p>
   </div>
   <div class="dialog-footer"><button type="button" class="button" :disabled="saving" @click="close">取消</button><button type="submit" class="button primary" :disabled="saving"><LoaderCircle v-if="saving" :size="16" class="spin" />{{saving ? '保存中…' : monitor ? '保存修改' : '创建监控'}}</button></div>
  </form>
 </dialog>
</template>
