<script setup>
import { reactive, ref, onMounted } from 'vue'
import { X, Server, Network, LoaderCircle } from 'lucide-vue-next'
import { agentState } from '../api/time.js'
import { validateFleet } from '../api/fleet.js'
const props = defineProps({ kind: String, item: Object, nodes: Array, onSave: Function })
const emit = defineEmits(['close'])
const dialog = ref(null), firstInput = ref(null), saving = ref(false), errors = ref({}), serverError = ref('')
const form = reactive({ name: props.item?.name || '', address: props.item?.address || '', region: props.item?.region || '', enabled: props.item?.enabled ?? true, nodeIds: [...(props.item?.nodeIds || props.nodes.filter(n => n.enabled).map(n => n.id))] })
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
  <h2 id="fleet-form-title">{{item ? '编辑' : '添加'}}{{kind === 'vps' ? ' VPS' : '测试节点'}}</h2>
  <p class="dialog-description">{{kind === 'vps' ? '填写服务器地址，选择从哪里测量延迟。' : '在另一台 Linux 服务器上安装探针，作为测量来源。'}}</p>
  <form novalidate @submit.prevent="submit">
   <div class="form-fields">
    <div class="field"><label for="fleet-name">名称 <span>*</span></label><input id="fleet-name" ref="firstInput" v-model="form.name" maxlength="100" :placeholder="kind === 'vps' ? '例如：香港 VPS' : '例如：上海测试节点'" :disabled="saving" :aria-invalid="Boolean(errors.name)" aria-describedby="fleet-name-error" /><small v-if="errors.name" id="fleet-name-error" class="field-error">{{errors.name}}</small></div>
    <div v-if="kind === 'vps'" class="field"><label for="fleet-address">VPS 地址 <span>*</span></label><input id="fleet-address" v-model="form.address" placeholder="IP 或主机名" maxlength="253" :disabled="saving" :aria-invalid="Boolean(errors.address)" aria-describedby="fleet-address-hint" /><small id="fleet-address-hint" :class="errors.address ? 'field-error' : 'field-hint'">{{errors.address || '只填 IP 或主机名；测试节点向这个地址发送 Ping。'}}</small><small v-if="item && form.address.trim() !== item.address" class="field-hint">更换目标地址后将清除该 VPS 的旧测量记录。</small></div>
    <div class="field"><label for="fleet-region">地区 <span class="optional">选填</span></label><input id="fleet-region" v-model="form.region" placeholder="例如：中国 · 香港" maxlength="100" :disabled="saving" :aria-invalid="Boolean(errors.region)" /><small v-if="errors.region" class="field-error">{{errors.region}}</small></div>
    <fieldset v-if="kind === 'vps'" class="node-picker"><legend>测试节点</legend><label v-for="node in nodes" :key="node.id"><input v-model="form.nodeIds" type="checkbox" :value="node.id" :disabled="saving" /><span>{{node.name}}<small>{{node.region || '未填写地区'}} · {{!node.enabled ? '已停用' : agentState(node, Date.now()) === 'ONLINE' ? '在线' : agentState(node, Date.now()) === 'PENDING' ? '待安装' : '离线'}}</small></span></label><p v-if="!nodes.length" class="field-hint">还没有测试节点。可以先保存 VPS，再到“测试节点”添加。</p><p class="field-hint">仅已启用且在线的节点参与检测；未选择节点时不会产生测量。</p><p v-if="errors.nodeIds" class="field-error">{{errors.nodeIds}}</p></fieldset>
    <label class="enable-field"><span><b>{{kind === 'vps' ? '启用监控' : '启用测试节点'}}</b><small>{{kind === 'vps' ? '允许所选节点执行测量' : '允许此节点参与测量'}}</small></span><input v-model="form.enabled" type="checkbox" role="switch" :aria-label="kind === 'vps' ? '启用监控' : '启用测试节点'" :disabled="saving" /><span class="switch-track" aria-hidden="true"></span></label>
    <p v-if="serverError" class="inline-error" role="alert">{{serverError}}</p>
   </div>
   <div class="dialog-footer"><button type="button" class="button" :disabled="saving" @click="close">取消</button><button type="submit" class="button primary" :disabled="saving"><LoaderCircle v-if="saving" :size="16" class="spin" />{{saving ? '保存中…' : item ? '保存修改' : '保存并查看安装'}}</button></div>
  </form>
 </dialog>
</template>
