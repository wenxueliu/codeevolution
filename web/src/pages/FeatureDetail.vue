<template>
  <div class="feature-detail" v-if="feature">
    <div class="back-row">
      <router-link to="/features" class="back-link">← 返回功能列表</router-link>
    </div>

    <div class="detail-header">
      <h1>{{ feature.canonical_name }}</h1>
      <div class="meta">
        <span class="type-tag">{{ feature.entry_type }}</span>
        <span class="status-tag" :class="feature.status">{{ feature.status }}</span>
        <span>{{ feature.event_count }} 个事件</span>
      </div>
      <div class="stable-id">Stable ID: {{ feature.stable_id }}</div>
    </div>

    <div class="timeline-section">
      <h3>演变时间线</h3>
      <div class="timeline" v-if="timeline.length">
        <div v-for="(ev, idx) in timeline" :key="idx" class="timeline-item">
          <div class="tl-dot" :class="ev.event_type"></div>
          <div class="tl-content">
            <div class="tl-header">
              <span class="event-badge" :class="ev.event_type">{{ ev.event_type }}</span>
              <span class="tl-date">{{ formatDate(ev.timestamp) }}</span>
              <span class="tl-commit">{{ ev.commit_hash?.slice(0, 7) }}</span>
            </div>
            <div class="tl-author">{{ ev.author }}</div>
            <div class="tl-message">{{ ev.message }}</div>
            <div v-if="ev.detail" class="tl-detail">
              <code>{{ formatDetail(ev.detail) }}</code>
            </div>
          </div>
        </div>
      </div>
      <p v-else class="empty">暂无时间线数据</p>
    </div>
  </div>
  <div v-else class="loading">加载中...</div>
</template>

<script>
export default {
  props: { stableId: String },
  data() { return { feature: null } },
  computed: {
    timeline() { return this.feature?.timeline || [] },
  },
  async created() { await this.loadFeature() },
  methods: {
    async loadFeature() {
      try {
        const r = await fetch('/api/features/' + encodeURIComponent(this.stableId));
        this.feature = await r.json();
      } catch(e) { console.error(e) }
    },
    formatDate(ts) {
      return new Date(ts * 1000).toLocaleDateString('zh-CN');
    },
    formatDetail(d) {
      if (typeof d === 'string') {
        try { d = JSON.parse(d) } catch(e) { return d }
      }
      return Object.entries(d).map(([k,v]) => `${k}: ${v}`).join(', ');
    },
  },
}
</script>

<style scoped>
.back-row { margin-bottom: 16px; }
.back-link { color: #888; text-decoration: none; font-size: 13px; }
.back-link:hover { color: #e94560; }
.detail-header { background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.detail-header h1 { font-size: 22px; margin-bottom: 12px; word-break: break-all; }
.meta { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; font-size: 14px; color: #666; }
.type-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; background: #e8e8e8; color: #555; }
.status-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.status-tag.active { background: #d4edda; color: #155724; }
.status-tag.removed { background: #f8d7da; color: #721c24; }
.stable-id { font-family: monospace; font-size: 12px; color: #aaa; }
.timeline-section { background: #fff; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.timeline-section h3 { font-size: 16px; margin-bottom: 20px; }
.timeline { position: relative; padding-left: 24px; border-left: 2px solid #eee; margin-left: 8px; }
.timeline-item { position: relative; margin-bottom: 20px; }
.tl-dot { position: absolute; left: -33px; top: 4px; width: 12px; height: 12px; border-radius: 50%; background: #ccc; border: 2px solid #fff; }
.tl-dot.BORN { background: #28a745; }
.tl-dot.DIED { background: #dc3545; }
.tl-dot.GROWN, .tl-dot.EXTENDED, .tl-dot.DEP_CREATED { background: #ffc107; }
.tl-dot.SHRUNK, .tl-dot.CONTRACTED, .tl-dot.DEP_REMOVED { background: #fd7e14; }
.tl-dot.MOVED { background: #6f42c1; }
.tl-dot.MODIFIED { background: #17a2b8; }
.tl-content { padding-left: 8px; }
.tl-header { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.event-badge { padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; background: #eee; color: #555; }
.event-badge.BORN { background: #d4edda; color: #155724; }
.event-badge.DIED { background: #f8d7da; color: #721c24; }
.event-badge.GROWN, .event-badge.EXTENDED, .event-badge.DEP_CREATED { background: #fff3cd; color: #856404; }
.event-badge.SHRUNK, .event-badge.CONTRACTED, .event-badge.DEP_REMOVED { background: #ffe5cc; color: #a0401a; }
.event-badge.MOVED { background: #e8d5f5; color: #5a2d8a; }
.tl-date { font-size: 12px; color: #999; }
.tl-commit { font-family: monospace; font-size: 11px; color: #bbb; }
.tl-author { font-size: 13px; color: #666; margin-bottom: 2px; }
.tl-message { font-size: 13px; color: #444; margin-bottom: 4px; }
.tl-detail code { font-size: 11px; color: #888; background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }
.loading { text-align: center; padding: 60px; color: #999; }
.empty { color: #999; text-align: center; padding: 40px 0; font-size: 14px; }
</style>
