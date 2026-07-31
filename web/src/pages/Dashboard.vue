<template>
  <div class="dashboard">
    <div v-if="error" class="request-error">{{ error.message }}</div>
    <div v-if="loading" class="request-loading">加载中...</div>
    <h1>仪表盘</h1>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-number">{{ stats.total_commits ?? '-' }}</div>
        <div class="stat-label">Commits 已分析</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ stats.total_features ?? '-' }}</div>
        <div class="stat-label">发现的功能</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ stats.active_features ?? '-' }}</div>
        <div class="stat-label">活跃功能</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ stats.total_events ?? '-' }}</div>
        <div class="stat-label">演变事件</div>
      </div>
    </div>

    <div class="charts-grid">
      <div class="chart-card">
        <h3>事件类型分布</h3>
        <div v-if="eventTypeStats.length" class="bar-chart">
          <div v-for="s in eventTypeStats" :key="s.event_type" class="bar-row">
            <span class="bar-label">{{ s.event_type }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: barWidth(s.count) }"></div>
            </div>
            <span class="bar-count">{{ s.count }}</span>
          </div>
        </div>
        <p v-else class="empty">暂无数据</p>
      </div>

      <div class="chart-card">
        <h3>最近事件</h3>
        <div v-if="recentEvents.length" class="event-list-compact">
          <div v-for="ev in recentEvents" :key="ev.id" class="event-row">
            <span class="event-badge" :class="ev.event_type">{{ ev.event_type }}</span>
            <span class="event-feature">{{ ev.canonical_name }}</span>
            <span class="event-commit">{{ ev.commit_hash?.slice(0, 7) }}</span>
            <span class="event-msg">{{ ev.message?.slice(0, 50) }}</span>
          </div>
        </div>
        <p v-else class="empty">暂无数据</p>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  props: { repoName: String },
  data() {
    return { stats: {}, eventTypeStats: [], recentEvents: [] }
  },
  async created() {
    await Promise.all([this.loadStats(), this.loadEventStats(), this.loadRecentEvents()])
  },
  methods: {
    async loadStats() {
      await this.$runAsync(async () => { this.stats = await this.$api.get('/api/stats', { repo: this.repoName || '' }) })
    },
    async loadEventStats() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/event-stats', { repo: this.repoName || '' })
        this.eventTypeStats = data.stats || [];
      })
    },
    async loadRecentEvents() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/events', { repo: this.repoName || '', limit: 10 })
        this.recentEvents = data.events || []
      })
    },
    barWidth(count) {
      const max = Math.max(...this.eventTypeStats.map(s => s.count));
      return max ? (count / max * 100) + '%' : '0%';
    },
  },
}
</script>

<style scoped>
.dashboard h1 { font-size: 24px; margin-bottom: 24px; }
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.stat-card { background: #fff; border-radius: 8px; padding: 20px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.stat-number { font-size: 32px; font-weight: 700; color: #e94560; }
.stat-label { font-size: 13px; color: #888; margin-top: 4px; }
.charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.chart-card { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); min-height: 200px; }
.chart-card h3 { font-size: 15px; margin-bottom: 16px; color: #333; }
.bar-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.bar-label { width: 120px; font-size: 12px; color: #555; text-align: right; flex-shrink: 0; }
.bar-track { flex: 1; height: 20px; background: #eee; border-radius: 4px; overflow: hidden; }
.bar-fill { height: 100%; background: #e94560; border-radius: 4px; transition: width 0.3s; min-width: 4px; }
.bar-count { font-size: 12px; color: #888; width: 30px; }
.event-list-compact { font-size: 13px; }
.event-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0; }
.event-badge { padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; background: #eee; color: #555; }
.event-badge.BORN { background: #d4edda; color: #155724; }
.event-badge.DIED { background: #f8d7da; color: #721c24; }
.event-badge.GROWN, .event-badge.EXTENDED { background: #fff3cd; color: #856404; }
.event-feature { color: #e94560; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.event-commit { font-family: monospace; color: #888; font-size: 11px; }
.event-msg { color: #aaa; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { color: #999; font-size: 14px; text-align: center; padding: 40px 0; }
</style>
