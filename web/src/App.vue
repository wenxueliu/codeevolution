<template>
  <div class="app">
    <nav class="nav">
      <div class="nav-brand">
        <router-link to="/">CodeHistory</router-link>
        <span class="nav-subtitle">代码仓功能演进分析</span>
      </div>
      <div class="nav-links">
        <router-link to="/">仪表盘</router-link>
        <router-link to="/features">功能列表</router-link>
        <router-link to="/events">事件日志</router-link>
      </div>
    </nav>
    <main class="main">
      <router-view />
    </main>
  </div>
</template>

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
.main { flex: 1; padding: 24px; max-width: 1400px; width: 100%; margin: 0 auto; }
</style>
