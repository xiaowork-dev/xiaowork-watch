<script setup>
import { reactive, ref, onMounted, watch } from 'vue'
import { X, Server, Network, LoaderCircle } from 'lucide-vue-next'
import { validateFleet } from '../api/fleet.js'
import { formatEndpoint, targetConfigured } from '../api/measurements.js'
const props = defineProps({ kind: String, item: Object, nodes: Array, onSave: Function })
const emit = defineEmits(['close'])
const dialog = ref(null), firstInput = ref(null), saving = ref(false), errors = ref({}), serverError = ref('')
const form = reactive({ name: props.item?.name || '', address: props.item?.address || '', protocol: props.item?.protocol || 'ICMP', port: props.item?.port ?? '', region: props.item?.region || '', enabled: props.item?.enabled ?? true, nodeIds: [...(props.item?.nodeIds || props.nodes.filter(n => n.enabled && targetConfigured(n)).map(n => n.id))] })
watch(() => form.protocol, value => { if (value === 'ICMP') form.port = '' })
onMounted(() => { dialog.value.showModal(); firstInput.value.focus() })
const close = () => { if (!saving.value) emit('close') }
async function submit() {
 if (saving.value) return
 const result = validateFleet(form, props.kind); errors.value = result.errors; serverError.value = ''
 if (Object.keys(result.errors).length) return
 saving.value = true
 try { await props.onSave(result.value); emit('close') } catch (error) { serverError.value = error.message } finally { saving.value = false }
}
</script>
<template>
 <dialog ref="dialog" class="form-dialog fleet-dialog" aria-labelledby="fleet-form-title" @cancel.prevent="close">
  <div class="dialog-heading"><div class="dialog-icon"><Server v-if="kind === 'vps'" :size="22" /><Network v-else :size="22" /></div><button class="icon-button" aria-label="关闭表单" :disabled="saving" @click="close"><X :size="20" /></button></div>
  <h2 id="fleet-form-title">{{item ? '编辑' : '添加'}}{{kind === 'vps' ? ' VPS' : '测速目标'}}</h2>
  <p class="dialog-description">{{kind === 'vps' ? 'VPS 上的探针向选定的外部目标测量出站延迟。' : '填写外部 DNS 主机名或 IP 地址；目标无需安装任何程序。'}}</p>
  <form novalidate @submit.prevent="submit">
   <div class="form-fields">
    <div class="field"><label for="fleet-name">名称 <span>*</span></label><input id="fleet-name" ref="firstInput" v-model="form.name" maxlength="100" :placeholder="kind === 'vps' ? '例如：香港 VPS' : '例如：湖北电信线路'" :disabled="saving" :aria-invalid="Boolean(errors.name)" aria-describedby="fleet-name-error" /><small v-if="errors.name" id="fleet-name-error" class="field-error">{{errors.name}}</small></div>
    <div v-if="kind !== 'vps'" class="field"><label for="target-protocol">测量协议</label><select id="target-protocol" v-model="form.protocol" :disabled="saving"><option value="ICMP">ICMP Ping · 丢包率</option><option value="TCP">TCP 连接 · 连接失败率</option></select><small v-if="errors.protocol" class="field-error">{{errors.protocol}}</small></div>
    <div class="field"><label for="fleet-address">{{kind === 'vps' ? 'VPS 地址' : '目标地址'}} <span>*</span></label><input id="fleet-address" v-model="form.address" :placeholder="kind === 'vps' ? 'IP 或主机名' : form.protocol === 'TCP' ? 'hb-ct-v4.ip.zstaticcdn.com:80' : 'IP 或主机名，不含端口'" maxlength="261" :disabled="saving" :aria-invalid="Boolean(errors.address)" aria-describedby="fleet-address-hint" /><small id="fleet-address-hint" :class="errors.address ? 'field-error' : 'field-hint'">{{errors.address || (kind === 'vps' ? '用于识别这台 VPS；测量由它主动向外部目标发起。' : form.protocol === 'TCP' ? '可填写主机名:端口或 [IPv6]:端口；不含 http:// 或路径。' : 'ICMP 不使用端口；仅填写公网 IP 或 DNS 主机名。')}}</small></div>
    <div v-if="kind !== 'vps' && form.protocol === 'TCP'" class="field"><label for="target-port">TCP 端口</label><input id="target-port" v-model="form.port" type="number" min="1" max="65535" step="1" placeholder="地址已带端口时可留空" :disabled="saving" :aria-invalid="Boolean(errors.port)" /><small v-if="errors.port" class="field-error">{{errors.port}}</small><small v-else class="field-hint">连接尝试不发送应用层数据。端口范围 1–65535。</small></div>
    <p v-else-if="errors.port" class="field-error" role="alert">{{errors.port}}</p>
    <p v-if="kind !== 'vps' && item?.needsConfiguration" class="installation-notice">这是旧版保留的目标，请补齐真实地址和协议，再启用它。原名称不会被推测为地址。</p>
    <p v-if="kind !== 'vps' && item && (form.address.trim() !== item.address || form.protocol !== item.protocol || Number(form.port || 0) !== Number(item.port || 0))" class="field-hint">修改地址、协议或端口后，只显示新配置的最新结果；已有历史保留原地址快照。</p>
    <div class="field"><label for="fleet-region">地区 <span class="optional">选填</span></label><input id="fleet-region" v-model="form.region" placeholder="例如：中国 · 湖北" maxlength="100" :disabled="saving" :aria-invalid="Boolean(errors.region)" /><small v-if="errors.region" class="field-error">{{errors.region}}</small></div>
    <fieldset v-if="kind === 'vps'" class="node-picker"><legend>测速目标</legend><label v-for="node in nodes" :key="node.id"><input v-model="form.nodeIds" type="checkbox" :value="node.id" :disabled="saving || (!targetConfigured(node) && !form.nodeIds.includes(node.id))" /><span>{{node.name}}<small>{{!targetConfigured(node) ? '待补目标地址' : node.protocol + ' · ' + formatEndpoint(node.address,node.protocol,node.port)}} · {{node.enabled ? '已启用' : '已停用'}}</small></span></label><p v-if="!nodes.length" class="field-hint">还没有测速目标。可以先保存 VPS，再到“测速目标”添加。</p><p class="field-hint">仅已启用且配置完整的目标参与测量；VPS 探针须在线并已升级。</p><p v-if="errors.nodeIds" class="field-error">{{errors.nodeIds}}</p></fieldset>
    <label class="enable-field"><span><b>{{kind === 'vps' ? '启用监控' : '启用测速目标'}}</b><small>{{kind === 'vps' ? '允许此 VPS 执行出站测量' : '允许关联 VPS 向此目标测量'}}</small></span><input v-model="form.enabled" type="checkbox" role="switch" :aria-label="kind === 'vps' ? '启用监控' : '启用测速目标'" :disabled="saving" /><span class="switch-track" aria-hidden="true"></span></label>
    <p v-if="serverError" class="inline-error" role="alert">{{serverError}}</p>
   </div>
   <div class="dialog-footer"><button type="button" class="button" :disabled="saving" @click="close">取消</button><button type="submit" class="button primary" :disabled="saving"><LoaderCircle v-if="saving" :size="16" class="spin" />{{saving ? '保存中…' : item ? '保存修改' : kind === 'vps' ? '保存并查看安装' : '保存目标'}}</button></div>
  </form>
 </dialog>
</template>