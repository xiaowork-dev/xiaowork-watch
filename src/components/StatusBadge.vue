<script setup>
import { computed } from 'vue'
const props = defineProps({ status: String, enabled: { type: Boolean, default: true }, showCode: Boolean })
const kind = computed(() => props.enabled ? (props.status || 'UNKNOWN').toLowerCase() : 'paused')
const label = computed(() => !props.enabled ? '已停用' : props.status === 'UP' ? '运行正常' : props.status === 'DOWN' ? '检测失败' : '尚未检测')
</script>
<template><span :class="['status-badge', kind]"><i class="status-dot"></i>{{label}}<span v-if="showCode && enabled" class="badge-code">{{status || 'UNKNOWN'}}</span></span></template>
