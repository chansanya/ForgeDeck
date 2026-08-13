/* 创建 Vue 应用并注册 Pinia、Router 与全局样式。 */

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles/base.css'
import '@xterm/xterm/css/xterm.css'

createApp(App).use(createPinia()).use(router).mount('#app')
