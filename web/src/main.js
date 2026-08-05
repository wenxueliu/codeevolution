import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import { apiClient } from './api/apiClient.js'
import { runAsync } from './composables/useAsync.js'

const Home = () => import('./pages/Home.vue')
const Dashboard = () => import('./pages/Dashboard.vue')
const FeatureList = () => import('./pages/FeatureList.vue')
const FeatureDetail = () => import('./pages/FeatureDetail.vue')
const EventList = () => import('./pages/EventList.vue')
const Capabilities = () => import('./pages/Capabilities.vue')
const Knowledge = () => import('./pages/Knowledge.vue')
const Refactoring = () => import('./pages/Refactoring.vue')

const routes = [
  { path: '/', name: 'home', component: Home },
  { path: '/repo/:repoName', name: 'dashboard', component: Dashboard, props: true },
  { path: '/repo/:repoName/features', name: 'features', component: FeatureList, props: true },
  { path: '/repo/:repoName/features/:stableId', name: 'feature-detail', component: FeatureDetail, props: true },
  { path: '/repo/:repoName/capabilities', name: 'capabilities', component: Capabilities, props: true },
  { path: '/repo/:repoName/knowledge', name: 'knowledge', component: Knowledge, props: true },
  { path: '/repo/:repoName/refactoring', name: 'refactoring', component: Refactoring, props: true },
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
