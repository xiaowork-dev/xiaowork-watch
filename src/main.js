import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import { initTheme, disposeTheme } from './theme.js'

initTheme()
const app = createApp(App)
app.onUnmount(disposeTheme)
app.mount('#app')
if (import.meta.hot) import.meta.hot.dispose(disposeTheme)
