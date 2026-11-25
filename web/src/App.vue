<template>
  <div class="app">
    <nav class="nav" :class="{ 'is-open': menuOpen }">
      <div class="nav-brand">
        <router-link to="/">CodeEvolution</router-link>
        <span class="nav-subtitle">{{ t('代码仓功能演进分析') }}</span>
      </div>
      <div id="primary-navigation" class="nav-menu">
        <div class="nav-links">
          <router-link :to="knowledgeRoute" @click="menuOpen = false">{{ t('知识中心') }}</router-link>
          <router-link to="/snapshots" @click="menuOpen = false">{{ t('Snapshots') }}</router-link>
          <router-link :to="graphViewRoute" @click="menuOpen = false">{{ t('Graph View') }}</router-link>
        </div>
        <div class="nav-right">
          <span v-if="repoName" class="nav-repo">{{ repoName }}</span>
          <button class="llm-settings-button" type="button" @click="menuOpen = false; settingsOpen = true">{{ t('LLM 设置') }}</button>
          <button class="locale-button" type="button" :aria-label="t(locale === 'zh' ? '切换到英文' : '切换到中文')" @click="menuOpen = false; toggleLanguage()">{{ locale === 'zh' ? 'EN' : '中' }}</button>
        </div>
      </div>
      <button
        class="nav-menu-button"
        type="button"
        :aria-label="t(menuOpen ? '关闭导航菜单' : '打开导航菜单')"
        :aria-expanded="menuOpen"
        aria-controls="primary-navigation"
        @click="toggleMenu"
      >
        <span aria-hidden="true">{{ menuOpen ? '×' : '☰' }}</span>
      </button>
    </nav>
    <div v-if="menuOpen" class="nav-scrim" aria-hidden="true" @click="menuOpen = false"></div>
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
  data: () => ({ settingsOpen: false, menuOpen: false, navigationContext: readNavigationContext(), locale: locale.value }),
  created() {
    window.addEventListener(NAVIGATION_CONTEXT_EVENT, this.refreshNavigationContext)
    window.addEventListener(LOCALE_EVENT, this.refreshLocale)
    window.addEventListener('keydown', this.onKeydown)
  },
  beforeUnmount() {
    window.removeEventListener(NAVIGATION_CONTEXT_EVENT, this.refreshNavigationContext)
    window.removeEventListener(LOCALE_EVENT, this.refreshLocale)
    window.removeEventListener('keydown', this.onKeydown)
  },
  watch: {
    $route() { this.menuOpen = false },
  },
  computed: {
    repoName() {
      return this.$route.params.repoName || ''
    },
    knowledgeRoute() {
      return { name: 'knowledge-home' }
    },
    graphViewRoute() {
      return { name: 'graph-view' }
    },
  },
  methods: {
    t,
    toggleLanguage() { setLocale(this.locale === 'zh' ? 'en' : 'zh') },
    toggleMenu() { this.menuOpen = !this.menuOpen },
    onKeydown(event) { if (event.key === 'Escape' && this.menuOpen) this.menuOpen = false },
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
  position: sticky; top: 0; z-index: 50;
  background: #1a1a2e; color: #fff; padding: 0 24px;
  display: flex; align-items: center; justify-content: space-between;
  height: 52px; flex-shrink: 0;
}
.nav-brand { display: flex; align-items: baseline; gap: 12px; }
.nav-brand a { color: #e94560; text-decoration: none; font-size: 18px; font-weight: 700; }
.nav-subtitle { font-size: 12px; color: #888; }
.nav-menu { display: contents; }
.nav-links { display: flex; gap: 20px; }
.nav-links a { color: #aaa; text-decoration: none; font-size: 14px; padding: 6px 0; border-bottom: 2px solid transparent; transition: all 0.2s; }
.nav-links a:hover, .nav-links a.router-link-active { color: #fff; border-bottom-color: #e94560; }
.nav-right { display: flex; align-items: center; }
.nav-repo { color: #e94560; font-size: 13px; font-weight: 600; }
.llm-settings-button { margin-left: 12px; border: 1px solid #4b4b61; background: transparent; color: #ddd; border-radius: 6px; padding: 6px 9px; cursor: pointer; white-space: nowrap; }
.locale-button { margin-left: 6px; border: 1px solid #4b4b61; background: transparent; color: #ddd; border-radius: 6px; padding: 6px 9px; cursor: pointer; font-weight: 600; }
.nav-menu-button { display: none; }
.nav-scrim { display: none; }
.main { flex: 1; padding: 24px; max-width: 1400px; width: 100%; margin: 0 auto; }
.request-error { margin-bottom: 16px; padding: 10px 14px; border-radius: 6px; background: #f8d7da; color: #721c24; }
.request-loading { margin-bottom: 12px; color: #666; font-size: 13px; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid #5c9ded; outline-offset: 2px; }
@media (max-width: 900px) {
  .nav { padding: 0 14px; }
  .nav-subtitle, .nav-repo { display: none; }
  .nav-right { display: flex; }
  .llm-settings-button { margin-left: 6px; padding: 6px 8px; }
  .main { padding: 16px; }
}
/* Phones and small tablets: collapse the links and actions into a slide-down
   menu so the brand and menu button never compete for horizontal space. */
@media (max-width: 820px) {
  .nav { height: 54px; padding: 0 12px; }
  .nav-brand a { font-size: 17px; }
  .nav-menu {
    display: none; position: absolute; top: 100%; left: 0; right: 0;
    flex-direction: column; align-items: stretch;
    max-height: calc(100dvh - 54px); overflow-y: auto;
    padding: 4px 14px calc(14px + env(safe-area-inset-bottom, 0px));
    background: #151526; border-top: 1px solid #2b2b40;
    box-shadow: 0 16px 30px rgba(8, 8, 16, 0.4);
  }
  .nav.is-open .nav-menu { display: flex; }
  .nav-links { flex-direction: column; gap: 0; }
  .nav-links a {
    padding: 13px 10px; font-size: 15px;
    border-bottom: 1px solid #2b2b40; border-left: 3px solid transparent;
  }
  .nav-links a:hover { background: #202034; }
  .nav-links a.router-link-active {
    color: #fff; background: #202034;
    border-bottom-color: #2b2b40; border-left-color: #e94560;
  }
  .nav-right { flex-wrap: wrap; gap: 8px; padding-top: 12px; border-top: 1px solid #2b2b40; }
  .nav-repo { display: block; flex-basis: 100%; margin-bottom: 4px; overflow-wrap: anywhere; }
  .llm-settings-button { margin-left: 0; flex: 1; min-height: 40px; }
  .locale-button { margin-left: 0; min-height: 40px; padding: 6px 16px; }
  .nav-menu-button {
    display: inline-flex; align-items: center; justify-content: center;
    width: 42px; height: 38px; margin-left: auto;
    border: 1px solid #4b4b61; border-radius: 8px;
    background: transparent; color: #fff; font-size: 20px; line-height: 1;
    cursor: pointer;
  }
  .nav-menu-button:hover { background: #2b2b40; }
  .nav-scrim {
    display: block; position: fixed; z-index: 40;
    top: 54px; right: 0; bottom: 0; left: 0;
    background: rgba(8, 8, 16, 0.45);
  }
  .main { padding: 14px 12px calc(30px + env(safe-area-inset-bottom, 0px)); }
}
</style>
