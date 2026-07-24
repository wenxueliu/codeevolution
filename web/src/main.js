import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import Dashboard from './pages/Dashboard.vue'
import FeatureList from './pages/FeatureList.vue'
import FeatureDetail from './pages/FeatureDetail.vue'
import EventList from './pages/EventList.vue'

const routes = [
  { path: '/', name: 'dashboard', component: Dashboard },
  { path: '/features', name: 'features', component: FeatureList },
  { path: '/features/:stableId', name: 'feature-detail', component: FeatureDetail, props: true },
  { path: '/events', name: 'events', component: EventList },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

const app = createApp(App)
app.use(router)
app.mount('#app')
