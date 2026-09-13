<template>
  <div class="app">
    <nav class="nav">
      <div class="nav-brand">
        <router-link to="/">CodeEvolution</router-link>
        <span class="nav-subtitle">{{ t('代码仓功能演进分析') }}</span>
      </div>
      <div class="nav-links">
        <router-link :to="knowledgeRoute">{{ t('知识中心') }}</router-link>
        <router-link to="/snapshots">{{ t('Snapshots') }}</router-link>
        <router-link :to="graphViewRoute">{{ t('Graph View') }}</router-link>
        <router-link :to="termsRoute">{{ t('术语') }}</router-link>
      </div>
      <div class="nav-right">
        <span v-if="repoName" class="nav-repo">{{ repoName }}</span>
        <button class="llm-settings-button" type="button" @click="settingsOpen = true">{{ t('LLM 设置') }}</button>
        <button class="locale-button" type="button" :aria-label="t(locale === 'zh' ? '切换到英文' : '切换到中文')" @click="toggleLanguage">{{ locale === 'zh' ? 'EN' : '中' }}</button>
      </div>
    </nav>
    <main class="main">
      <router-view />
    </main>
    <RepositoryAssistant :repo-name="repoName" />
    <LLMSettings :open="settingsOpen" @close="settingsOpen = false" />
  </div>
</template>

<script>
import RepositoryAssistant from './components/RepositoryAssistant.vue'
import LLMSettings from './components/LLMSettings.vue'
import { NAVIGATION_CONTEXT_EVENT, readNavigationContext } from './navigationContext.js'
import { LOCALE_EVENT, locale, setLocale, t } from './i18n.js'

export default {
  components: { LLMSettings, RepositoryAssistant },
  data: () => ({ settingsOpen: false, navigationContext: readNavigationContext(), locale: locale.value }),
  created() {
    window.addEventListener(NAVIGATION_CONTEXT_EVENT, this.refreshNavigationContext)
    window.addEventListener(LOCALE_EVENT, this.refreshLocale)
  },
  beforeUnmount() {
    window.removeEventListener(NAVIGATION_CONTEXT_EVENT, this.refreshNavigationContext)
    window.removeEventListener(LOCALE_EVENT, this.refreshLocale)
  },
  computed: {
    repoName() {
      return this.$route.params.repoName || ''
    },
    graphViewId() {
      return this.$route.params.viewId || ''
    },
    knowledgeRoute() {
      return { name: 'knowledge-home' }
    },
    graphViewRoute() {
      return { name: 'graph-view' }
    },
    termsRoute() {
      const query = {}
      if (this.$route.query.snapshot_id) query.snapshot_id = this.$route.query.snapshot_id
      if (this.graphViewId) query.view_id = this.graphViewId
      return { name: 'terms', query }
    },
  },
  methods: {
    t,
    toggleLanguage() { setLocale(this.locale === 'zh' ? 'en' : 'zh') },
    refreshLocale(event) { this.locale = event?.detail || locale.value },
    refreshNavigationContext(event) {
      this.navigationContext = event?.detail || readNavigationContext()
    },
  },
}
</script>

<style>
.app { min-height: 100vh; display: flex; flex-direction: column; }
.nav {
  background: #1a1a2e; color: #fff; padding: 0 24px;
  display: flex; align-items: center; justify-content: space-between;
  height: 52px; flex-shrink: 0;
}
.nav-brand { display: flex; align-items: baseline; gap: 12px; }
.nav-brand a { color: #e94560; text-decoration: none; font-size: 18px; font-weight: 700; }
.nav-subtitle { font-size: 12px; color: #888; }
.nav-links { display: flex; gap: 20px; }
.nav-links a { color: #aaa; text-decoration: none; font-size: 14px; padding: 6px 0; border-bottom: 2px solid transparent; transition: all 0.2s; }
.nav-links a:hover, .nav-links a.router-link-active { color: #fff; border-bottom-color: #e94560; }
.nav-right { display: flex; align-items: center; }
.nav-repo { color: #e94560; font-size: 13px; font-weight: 600; }
.llm-settings-button { margin-left: 12px; border: 1px solid #4b4b61; background: transparent; color: #ddd; border-radius: 6px; padding: 6px 9px; cursor: pointer; white-space: nowrap; }
.locale-button { margin-left: 6px; border: 1px solid #4b4b61; background: transparent; color: #ddd; border-radius: 6px; padding: 6px 9px; cursor: pointer; font-weight: 600; }
.main { flex: 1; padding: 24px; max-width: 1400px; width: 100%; margin: 0 auto; }
.request-error { margin-bottom: 16px; padding: 10px 14px; border-radius: 6px; background: #f8d7da; color: #721c24; }
.request-loading { margin-bottom: 12px; color: #666; font-size: 13px; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid #5c9ded; outline-offset: 2px; }
@media (max-width: 900px) {
  .nav { padding: 0 14px; position: relative; }
  .nav-subtitle, .nav-repo { display: none; }
  .nav-right { display: flex; }
  .llm-settings-button { margin-left: 6px; padding: 6px 8px; }
  .main { padding: 16px; }
}
</style>
