<script setup>
import { ref } from 'vue'
import { LockKeyhole, LoaderCircle } from 'lucide-vue-next'
import { authApi } from '../api/client.js'
defineProps({ sessionError: String })
const emit = defineEmits(['success'])
const username = ref('admin'), password = ref(''), submitting = ref(false), error = ref('')
async function submit() {
 if (submitting.value) return
 error.value = ''
 if (!username.value.trim() || !password.value) { error.value = '请输入管理员用户名和密码'; return }
 submitting.value = true
 try { await authApi.login(username.value.trim(), password.value); emit('success') }
 catch (e) { error.value = e.message }
 finally { password.value = ''; submitting.value = false }
}
</script>
<template>
 <section class="login-panel" aria-labelledby="login-title">
  <div class="dialog-icon"><LockKeyhole :size="23" /></div><h1 id="login-title">管理员登录</h1><p class="dialog-description">登录后可管理监控、探针和测量任务。</p>
  <form @submit.prevent="submit">
   <div class="form-fields">
    <div class="field"><label for="admin-username">用户名</label><input id="admin-username" v-model="username" autocomplete="username" maxlength="100" required :disabled="submitting" /></div>
    <div class="field"><label for="admin-password">密码</label><input id="admin-password" v-model="password" type="password" autocomplete="current-password" required :disabled="submitting" /></div>
    <p v-if="error || sessionError" class="inline-error" role="alert">{{error || sessionError}}</p>
    <button class="button primary" type="submit" :disabled="submitting"><LoaderCircle v-if="submitting" :size="17" class="spin" />{{submitting ? '正在登录…' : '登录后台'}}</button>
   </div>
  </form>
  <p class="login-help">默认用户名为 admin。随机密码由服务器安装菜单显示；忘记密码请在主控服务器终端重置管理员密码。重置后原会话失效。</p>
 </section>
</template>
