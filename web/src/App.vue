<template>
  <div class="app">
    <nav class="nav">
      <div class="nav-brand">
        <router-link to="/">CodeHistory</router-link>
        <span class="nav-subtitle">代码仓功能演进分析</span>
      </div>
      <button v-if="repoName" class="nav-menu-button" type="button" :aria-expanded="menuOpen" aria-controls="primary-navigation" @click="menuOpen = !menuOpen">
        <span aria-hidden="true">☰</span><span>菜单</span>
      </button>
      <div id="primary-navigation" class="nav-links" :class="{ open: menuOpen }" v-if="repoName" @click="menuOpen = false">
        <router-link :to="'/repo/' + repoName" exact-active-class="router-link-active">仪表盘</router-link>
        <router-link :to="'/repo/' + repoName + '/features'">功能列表</router-link>
        <router-link :to="'/repo/' + repoName + '/capabilities'">特性聚类</router-link>
        <router-link :to="'/repo/' + repoName + '/knowledge'">知识中心</router-link>
        <router-link :to="'/repo/' + repoName + '/refactoring'">渐进重构</router-link>
        <router-link :to="'/repo/' + repoName + '/events'">事件日志</router-link>
      </div>
      <div class="nav-right">
        <span v-if="repoName" class="nav-repo">{{ repoName }}</span>
        <button class="llm-settings-button" type="button" @click="settingsOpen = true">LLM 设置</button>
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

export default {
  components: { LLMSettings, RepositoryAssistant },
  data: () => ({ menuOpen: false, settingsOpen: false }),
  computed: {
    repoName() {
      return this.$route.params.repoName || ''
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
.nav-menu-button { display: none; border: 1px solid #4b4b61; background: transparent; color: #fff; border-radius: 6px; padding: 6px 9px; cursor: pointer; gap: 6px; align-items: center; }
.main { flex: 1; padding: 24px; max-width: 1400px; width: 100%; margin: 0 auto; }
.request-error { margin-bottom: 16px; padding: 10px 14px; border-radius: 6px; background: #f8d7da; color: #721c24; }
.request-loading { margin-bottom: 12px; color: #666; font-size: 13px; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid #5c9ded; outline-offset: 2px; }
@media (max-width: 900px) {
  .nav { padding: 0 14px; position: relative; }
  .nav-subtitle, .nav-repo { display: none; }
  .nav-right { display: flex; }
  .llm-settings-button { margin-left: 6px; padding: 6px 8px; }
  .nav-menu-button { display: flex; }
  .nav-links { display: none; position: absolute; z-index: 40; top: 52px; left: 0; right: 0; padding: 8px 14px 14px; background: #1a1a2e; box-shadow: 0 8px 18px #0004; flex-direction: column; gap: 2px; }
  .nav-links.open { display: flex; }
  .nav-links a { padding: 10px 6px; border-bottom-width: 1px; }
  .main { padding: 16px; }
}
</style>
