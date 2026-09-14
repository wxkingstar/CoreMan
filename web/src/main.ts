import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/theme.css'
import './styles/controls.css'
import './styles/layout.css'
import './styles/overlays.css'
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { i18n } from './i18n'
import { router } from './router'

try { document.documentElement.classList.toggle('dark', localStorage.getItem('coreman.dark') === '1') } catch { /* storage unavailable */ }

document.documentElement.lang = i18n.global.locale.value === 'zh' ? 'zh-CN' : i18n.global.locale.value
createApp(App).use(createPinia()).use(router).use(i18n).use(ElementPlus).mount('#app')
