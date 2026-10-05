<script setup>
import { onBeforeUnmount, ref } from 'vue'
import { ChevronDown, Monitor, Sun, Moon } from 'lucide-vue-next'
import { getThemeController } from '../theme.js'

const emit = defineEmits(['change'])
const controller = getThemeController()
const preference = ref(controller.getSnapshot().preference)
const options = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
]
const unsubscribe = controller.subscribe((snapshot) => {
  preference.value = snapshot.preference
})
onBeforeUnmount(unsubscribe)

function select(value) {
  controller.setPreference(value)
  emit('change', value)
}
</script>

<template>
  <div class="theme-switch">
    <Sun v-if="preference === 'light'" class="theme-icon" :size="17" aria-hidden="true" />
    <Moon v-else-if="preference === 'dark'" class="theme-icon" :size="17" aria-hidden="true" />
    <Monitor v-else class="theme-icon" :size="17" aria-hidden="true" />
    <select
      class="theme-select"
      aria-label="显示主题"
      :value="preference"
      @change="select($event.target.value)"
    >
      <option v-for="option in options" :key="option.value" :value="option.value">
        {{ option.label }}
      </option>
    </select>
    <ChevronDown class="theme-chevron" :size="14" aria-hidden="true" focusable="false" />
  </div>
</template>
