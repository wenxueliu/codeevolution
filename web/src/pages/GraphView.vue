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
        <div><small>Artifact</small><span>{{ artifact ? '已生成' : '未生成' }}</span></div>
        <div><small>服务</small><span>{{ services.length || '—' }}</span></div>
        <div><small>调用边</small><span>{{ serviceEdges.length || '—' }}</span></div>
      </section>

      <section v-if="!artifact" class="panel artifact-empty">
        <h2>尚未生成拓扑 Artifact</h2>
        <p class="muted">Graph View 只固定 Scope 和 Snapshot；拓扑分析需要显式创建一次 Artifact Job。</p>
        <button class="primary" :disabled="artifactLoading" @click="generate">{{ artifactLoading ? '正在创建…' : '生成拓扑' }}</button>
        <p v-if="jobStatus" class="muted">Job {{ jobStatus.id }}：{{ jobStatus.status }}</p>
      </section>
      <UiState v-else-if="!services.length" kind="empty" title="此 View 没有可浏览的服务" message="当前 Artifact 没有可分析的 Scope 成员。" />
      <template v-else>
        <section class="panel">
          <h2>服务拓扑</h2>
          <p class="muted">边仅来自此 View 固定快照中的通信事实；点击服务查看影响和流程。</p>
          <div class="service-list" aria-label="服务列表">
            <button v-for="service in services" :key="service.member_id" :class="{ active: selectedService === service.member_id }" @click="selectService(service.member_id)">
              {{ service.display_name || service.member_id }} <small>{{ service.availability || `${service.entries?.length || 0} entries` }}</small>
            </button>
          </div>
          <div v-if="serviceEdges.length" class="edge-list">
            <article v-for="(edge, index) in serviceEdges" :key="edgeKey(edge, index)" class="edge-card">
              <div class="edge-heading"><b>{{ edge.source_member_id }}</b><span>→</span><b>{{ targetName(edge) }}</b><span class="badge">{{ edge.kind }}</span><span v-if="edge.confidence" class="confidence">{{ edge.confidence }}</span></div>
              <p v-if="edge.source_entry_id || edge.target_entry_id"><code>{{ edge.source_entry_id || '—' }}</code> → <code>{{ edge.target_entry_id || '—' }}</code></p>
              <details v-if="edge.evidence"><summary>调用证据</summary><pre>{{ formatJson(edge.evidence) }}</pre></details>
              <details v-else-if="edge.dependency"><summary>依赖证据</summary><pre>{{ formatJson(edge.dependency) }}</pre></details>
            </article>
          </div>
          <p v-else class="muted">未在当前快照中发现跨服务调用边。</p>
          <details v-if="artifact.coverage" open><summary>覆盖情况</summary><pre>{{ formatJson(artifact.coverage) }}</pre></details>
        </section>

        <section class="panel analysis-panel">
          <div class="analysis-heading"><div><h2>影响与流程</h2><p class="muted">选择服务后，以同一 <code>view_id</code> 查询。</p></div><label>起始 API（可选）<span class="entry-input"><input v-model="method" placeholder="GET" @change="loadAnalysis" /><input v-model="path" placeholder="/api/orders" @change="loadAnalysis" /></span></label></div>
          <UiState v-if="analysisError" kind="error" title="分析加载失败" :message="analysisError.message" action-label="重试" @action="loadAnalysis" />
          <div v-else-if="analysisLoading" class="muted">正在查询影响与流程…</div>
          <template v-else>
            <div class="analysis-columns">
              <div><h3>{{ selectedService }} 的下游依赖</h3><ul><li v-for="item in impact.downstream_dependencies || []" :key="item.member_id">{{ item.member_id }} <small>{{ item.path?.join(' → ') }}</small></li></ul><p v-if="!(impact.downstream_dependencies || []).length" class="muted">没有确认的下游服务。</p></div>
              <div><h3>上游依赖方</h3><ul><li v-for="item in impact.upstream_dependents || []" :key="item.member_id">{{ item.member_id }} <small>{{ item.path?.join(' → ') }}</small></li></ul><p v-if="!(impact.upstream_dependents || []).length" class="muted">没有确认的上游服务。</p></div>
            </div>
            <details v-if="flow.nodes?.length" open><summary>静态流程（{{ flow.nodes.length }} 个节点）</summary><pre>{{ formatJson(flow) }}</pre></details>
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
  data() { return { artifact: null, selectedService: '', impact: {}, flow: {}, method: 'GET', path: '', loading: false, artifactLoading: false, analysisLoading: false, error: null, analysisError: null, jobStatus: null, pollTimer: null } },
  computed: {
    services() { return this.artifact?.services || [] },
    serviceEdges() { return this.artifact?.service_projections || [] },
  },
  watch: { viewId() { this.load() } },
  async created() { await this.load() },
  beforeUnmount() { this.stopPolling() },
  methods: {
    async load() {
      this.loading = true; this.error = null
      try {
        const response = await this.$api.get(`/api/graph-views/${this.viewId}/artifacts/topology`)
        this.artifact = response.artifact
        const first = this.services[0]?.member_id
        if (first) await this.selectService(first)
      } catch (error) {
        if (error.status === 404 && error.body?.detail === 'artifact_not_generated') this.artifact = null
        else this.error = error
      } finally { this.loading = false }
    },
    async generate() {
      this.artifactLoading = true; this.error = null
      try {
        const response = await this.$api.request(`/api/graph-views/${this.viewId}/artifact-jobs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artifact_kind: 'topology', params: {} }) })
        this.jobStatus = response.job
        if (response.job?.status === 'completed') await this.load()
        else this.startPolling(response.job?.id)
      } catch (error) { this.error = error } finally { this.artifactLoading = false }
    },
    startPolling(jobId) {
      this.stopPolling()
      if (!jobId) return
      const poll = async () => {
        try {
          const response = await this.$api.get(`/api/graph-artifact-jobs/${jobId}`)
          this.jobStatus = response.job
          if (['completed', 'failed', 'cancelled', 'interrupted'].includes(response.job?.status)) {
            this.stopPolling()
            if (response.job.status === 'completed') await this.load()
            else this.error = new Error(response.job.error_message || `Artifact Job ${response.job.status}`)
          }
        } catch (error) { this.stopPolling(); this.error = error }
      }
      this.pollTimer = window.setInterval(poll, 2000)
      poll()
    },
    stopPolling() { if (this.pollTimer) { window.clearInterval(this.pollTimer); this.pollTimer = null } },
    async selectService(service) { this.selectedService = service; await this.loadAnalysis() },
    async loadAnalysis() {
      if (!this.selectedService) return
      this.analysisLoading = true; this.analysisError = null
      try {
        const query = { member_id: this.selectedService }
        const impact = await this.$api.get(`/api/graph-views/${this.viewId}/impact`, query)
        this.impact = impact
        this.flow = this.path
          ? await this.$api.get(`/api/graph-views/${this.viewId}/flow`, { ...query, method: this.method || 'GET', path: this.path })
          : {}
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
.artifact-empty { text-align: center; }.primary { border: 0; border-radius: 5px; padding: 9px 15px; color: #fff; background: #315d9b; cursor: pointer; }.primary:disabled { opacity: .6; cursor: wait; }.entry-input { display: flex; gap: 6px; }.entry-input input:first-child { width: 58px; }
@media (max-width: 700px) { .view-meta, .analysis-columns { grid-template-columns: 1fr; }.analysis-heading { display: block; }.analysis-heading label { margin-top: 12px; } }
</style>
