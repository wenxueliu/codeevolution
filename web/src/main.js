import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import 'element-plus/theme-chalk/base.css'
import App from './App.vue'
import { apiClient } from './api/apiClient.js'
import { runAsync } from './composables/useAsync.js'

const Home = () => import('./pages/Home.vue')
const Knowledge = () => import('./pages/Knowledge.vue')
const Snapshots = () => import('./pages/Snapshots.vue')

const routes = [
  { path: '/', name: 'home', component: Home },
  { path: '/snapshots', name: 'snapshots', component: Snapshots },
  { path: '/repo/:repoName', name: 'knowledge', component: Knowledge, props: true },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

const app = createApp(App)
app.config.globalProperties.$api = apiClient
app.mixin({
  data: () => ({ loading: false, error: null }),
  methods: {
    $runAsync(task) { return runAsync(this, task) },
  },
})
app.use(router)
app.mount('#app')
