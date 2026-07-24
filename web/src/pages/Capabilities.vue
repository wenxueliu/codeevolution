<template>
  <div class="capabilities">
    <h1>特性聚类</h1>
    <p class="subtitle">基于类/模块分组 + 调用链重叠合并</p>

    <div class="cap-grid">
      <div v-for="cap in capabilities" :key="cap.id" class="cap-card">
        <div class="cap-header">
          <h2>{{ cap.name }}</h2>
          <span class="cap-zh">{{ cap.name_zh }}</span>
        </div>

        <div class="cap-meta">
          <span>{{ cap.feature_count }} 个功能</span>
          <span>{{ cap.event_count }} 个事件</span>
          <span v-if="cap.stats.max_call_depth">最大调用深度 {{ cap.stats.max_call_depth }}</span>
        </div>

        <div class="cap-modules" v-if="cap.modules.length > 1">
          <span class="module-label">跨模块:</span>
          <span v-for="m in cap.modules" :key="m" class="module-tag">{{ m }}</span>
        </div>
        <div class="cap-module" v-else>
          {{ cap.module }}
        </div>

        <!-- Merge reason for multi-class capabilities -->
        <div v-if="cap.modules.length > 1" class="merge-reason">
          合并原因：调用链末端共享 callee
        </div>

        <div class="cap-features">
          <div v-for="f in cap.features" :key="f.stable_id" class="cap-feature">
            <router-link :to="'/repo/' + repoName + '/features/' + encodeURIComponent(f.stable_id)">
              {{ f.canonical_name }}
            </router-link>
            <span class="feat-sig">{{ f.entry_signature }}</span>
          </div>
        </div>
      </div>
    </div>

    <div v-if="capabilities.length === 0" class="empty">暂无聚类数据</div>
  </div>
</template>

<script>
export default {
  props: { repoName: String },
  data() { return { capabilities: [] } },
  async created() { await this.load() },
  methods: {
    async load() {
      try {
        const r = await fetch('/api/capabilities?repo=' + encodeURIComponent(this.repoName || ''))
        const data = await r.json()
        this.capabilities = data.capabilities || []
      } catch(e) { console.error(e) }
    },
  },
}
</script>

<style scoped>
.capabilities h1 { font-size: 24px; margin-bottom: 4px; }
.subtitle { font-size: 13px; color: #999; margin-bottom: 24px; }
.cap-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }
.cap-card {
  background: #fff; border-radius: 8px; padding: 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.cap-header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.cap-header h2 { font-size: 17px; color: #e94560; }
.cap-zh { font-size: 12px; color: #999; }
.cap-meta { display: flex; gap: 16px; font-size: 12px; color: #888; margin-bottom: 6px; }
.cap-module { font-size: 12px; color: #aaa; font-family: monospace; margin-bottom: 8px; }
.cap-modules { font-size: 12px; margin-bottom: 6px; }
.module-label { color: #888; }
.module-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; background: #f0f0f0; color: #666; margin: 2px 4px 2px 0; font-family: monospace; font-size: 10px; }
.merge-reason { font-size: 11px; color: #ff9800; background: #fff8e1; padding: 4px 8px; border-radius: 4px; margin-bottom: 10px; }
.cap-features { border-top: 1px solid #f0f0f0; padding-top: 8px; }
.cap-feature { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; font-size: 13px; }
.cap-feature a { color: #333; text-decoration: none; }
.cap-feature a:hover { color: #e94560; text-decoration: underline; }
.feat-sig { font-size: 11px; color: #ccc; font-family: monospace; }
.empty { text-align: center; padding: 60px; color: #999; }
</style>
