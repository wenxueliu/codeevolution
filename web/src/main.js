import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import Home from './pages/Home.vue'
import Dashboard from './pages/Dashboard.vue'
import FeatureList from './pages/FeatureList.vue'
import FeatureDetail from './pages/FeatureDetail.vue'
import EventList from './pages/EventList.vue'
import Capabilities from './pages/Capabilities.vue'
import Knowledge from './pages/Knowledge.vue'
import { apiClient } from './api/apiClient.js'
import { runAsync } from './composables/useAsync.js'

const routes = [
  { path: '/', name: 'home', component: Home },
  { path: '/repo/:repoName', name: 'dashboard', component: Dashboard, props: true },
  { path: '/repo/:repoName/features', name: 'features', component: FeatureList, props: true },
  { path: '/repo/:repoName/features/:stableId', name: 'feature-detail', component: FeatureDetail, props: true },
  { path: '/repo/:repoName/capabilities', name: 'capabilities', component: Capabilities, props: true },
  { path: '/repo/:repoName/knowledge', name: 'knowledge', component: Knowledge, props: true },
  { path: '/repo/:repoName/events', name: 'events', component: EventList, props: true },
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
