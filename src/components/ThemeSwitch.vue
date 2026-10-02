<script setup>
import { onBeforeUnmount, ref, useId } from 'vue'
import { getThemeController } from '../theme.js'

const emit = defineEmits(['change'])
const controller = getThemeController()
const preference = ref(controller.getSnapshot().preference)
const name = `theme-${useId()}`
const options = [
  { value: 'light', label: '日间' },
  { value: 'dark', label: '夜间' },
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
  <fieldset class="theme-switch" aria-label="显示主题">
    <legend class="sr-only">显示主题</legend>
    <label
      v-for="option in options"
      :key="option.value"
      class="theme-choice"
      :class="{ 'is-active': preference === option.value }"
    >
      <input
        class="theme-radio"
        type="radio"
        :name="name"
        :value="option.value"
        :checked="preference === option.value"
        @change="select(option.value)"
      />
      <span>{{ option.label }}</span>
    </label>
  </fieldset>
</template>
