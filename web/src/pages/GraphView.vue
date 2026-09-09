<template>
  <div class="graph-view-page">
    <div class="page-header">
      <div>
        <h1>Graph View</h1>
        <p>以固定的快照集合浏览跨仓服务拓扑、变更影响和调用流程。</p>
      </div>
      <router-link class="secondary" :to="{ name: 'snapshots' }">返回 Snapshots</router-link>
    </div>

    <UiState v-if="error" kind="error" title="Graph View 加载失败" :message="error.message" action-label="重试" @action="load" />
    <UiState v-else-if="loading" kind="loading" title="正在加载 Graph View" />
    <template v-else>
      <section class="view-meta">
        <div><small>View ID</small><code>{{ viewId }}</code></div>
        <div><small>完整性</small><span>{{ topology.completeness || '—' }}</span></div>
        <div><small>服务</small><span>{{ services.length }}</span></div>
        <div><small>调用边</small><span>{{ topology.edges?.length || 0 }}</span></div>
      </section>

      <UiState v-if="!services.length" kind="empty" title="此 View 没有可浏览的服务" message="请先为 Scope 成员生成快照并创建新的 Graph View。" />
      <template v-else>
        <section class="panel">
          <h2>服务拓扑</h2>
          <p class="muted">边仅来自此 View 固定快照中的 API 调用链；点击服务查看影响和流程。</p>
          <div class="service-list" aria-label="服务列表">
            <button v-for="service in services" :key="service.member_id" :class="{ active: selectedService === service.member_id }" @click="selectService(service.member_id)">
              {{ service.member_id }} <small>{{ service.availability || `${service.endpoints?.length || 0} APIs` }}</small>
            </button>
          </div>
          <div v-if="topology.edges?.length" class="edge-list">
            <article v-for="(edge, index) in topology.edges" :key="edgeKey(edge, index)" class="edge-card">
              <div class="edge-heading"><b>{{ edge.source_member_id }}</b><span>→</span><b>{{ targetName(edge) }}</b><span class="badge">{{ edge.kind }}</span><span v-if="edge.confidence" class="confidence">{{ edge.confidence }}</span></div>
              <p v-if="edge.source_endpoint || edge.target_endpoint || edge.target_path"><code>{{ edge.source_endpoint || '—' }}</code> → <code>{{ edge.target_endpoint || edge.target_path || '—' }}</code></p>
              <details v-if="edge.evidence"><summary>调用证据</summary><pre>{{ formatJson(edge.evidence) }}</pre></details>
              <details v-else-if="edge.dependency"><summary>依赖证据</summary><pre>{{ formatJson(edge.dependency) }}</pre></details>
            </article>
          </div>
          <p v-else class="muted">未在当前快照中发现跨服务调用边。</p>
        </section>

        <section class="panel analysis-panel">
          <div class="analysis-heading"><div><h2>影响与流程</h2><p class="muted">选择服务后，以同一 <code>view_id</code> 查询。</p></div><label>起始 API（可选）<input v-model="path" placeholder="/api/orders" @change="loadAnalysis" /></label></div>
          <UiState v-if="analysisError" kind="error" title="分析加载失败" :message="analysisError.message" action-label="重试" @action="loadAnalysis" />
          <div v-else-if="analysisLoading" class="muted">正在查询影响与流程…</div>
          <template v-else>
            <div class="analysis-columns">
              <div><h3>{{ selectedService }} 的影响范围</h3><ul><li v-for="item in impact.affected || []" :key="item.member_id">{{ item.member_id }}</li></ul><p v-if="!(impact.affected || []).length" class="muted">没有受影响的服务。</p></div>
              <div><h3>流程步骤</h3><ol><li v-for="item in flow.steps || []" :key="item.member_id">{{ item.member_id }}</li></ol><p v-if="!(flow.steps || []).length" class="muted">没有可追踪的跨服务流程。</p></div>
            </div>
            <details v-if="impact.edges?.length" open><summary>影响关联边（{{ impact.edges.length }}）</summary><pre>{{ formatJson(impact.edges) }}</pre></details>
          </template>
        </section>
      </template>
    </template>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'

export default {
  components: { UiState },
  props: { viewId: { type: String, required: true } },
  data() { return { topology: { services: [], edges: [] }, selectedService: '', impact: {}, flow: {}, path: '', loading: false, analysisLoading: false, error: null, analysisError: null } },
  computed: { services() { return this.topology.services || [] } },
  watch: { viewId() { this.load() } },
  async created() { await this.load() },
  methods: {
    async load() {
      this.loading = true; this.error = null
      try {
        this.topology = await this.$api.get('/api/topology', { view_id: this.viewId })
        const first = this.services[0]?.member_id
        if (first) await this.selectService(first)
      } catch (error) { this.error = error } finally { this.loading = false }
    },
    async selectService(service) { this.selectedService = service; await this.loadAnalysis() },
    async loadAnalysis() {
      if (!this.selectedService) return
      this.analysisLoading = true; this.analysisError = null
      try {
        const query = { view_id: this.viewId, service: this.selectedService }
        const flowQuery = { ...query, path: this.path }
        const [impact, flow] = await Promise.all([this.$api.get('/api/impact', query), this.$api.get('/api/flow', flowQuery)])
        this.impact = impact; this.flow = flow
      } catch (error) { this.analysisError = error } finally { this.analysisLoading = false }
    },
    targetName(edge) { return edge.target_member_id || (edge.target_candidates || []).join(', ') || '外部依赖' },
    edgeKey(edge, index) { return `${edge.source_member_id}-${edge.target_member_id || edge.target_path || index}-${index}` },
    formatJson(value) { return JSON.stringify(value, null, 2) },
  },
}
</script>

<style scoped>
.view-meta, .analysis-columns { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }
.view-meta > div, .edge-card { padding: 12px; border: 1px solid var(--border, #ddd); border-radius: 6px; background: #fff; }
.view-meta small { display: block; color: #777; margin-bottom: 4px; }.view-meta code { overflow-wrap: anywhere; }
.panel { margin-bottom: 20px; padding: 18px; border: 1px solid var(--border, #ddd); border-radius: 8px; }.panel h2, .panel h3 { margin-top: 0; }
.service-list { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0; }.service-list button { border: 1px solid #c7d2e3; background: #f6f8fb; border-radius: 5px; padding: 8px 10px; cursor: pointer; }.service-list button.active { color: #fff; background: #315d9b; border-color: #315d9b; }.service-list small { margin-left: 5px; opacity: .75; }
.edge-list { display: grid; gap: 10px; }.edge-card p { margin: 8px 0; }.edge-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }.badge, .confidence { padding: 2px 6px; border-radius: 10px; font-size: 12px; background: #e8f0fc; }.confidence { background: #eef2f5; } details { margin-top: 8px; } summary { cursor: pointer; } pre { overflow: auto; padding: 10px; background: #f6f8fa; border-radius: 4px; font-size: 12px; }
.analysis-heading { display: flex; justify-content: space-between; gap: 20px; align-items: start; }.analysis-heading label { display: grid; gap: 4px; font-size: 13px; }.analysis-heading input { padding: 7px; border: 1px solid #bbb; border-radius: 4px; }.analysis-columns { grid-template-columns: repeat(2, minmax(0, 1fr)); }.analysis-columns > div { padding: 12px; background: #f8fafc; border-radius: 6px; }.analysis-columns ul, .analysis-columns ol { margin-bottom: 0; }.muted { color: #667085; }
@media (max-width: 700px) { .view-meta, .analysis-columns { grid-template-columns: 1fr; }.analysis-heading { display: block; }.analysis-heading label { margin-top: 12px; } }
</style>
