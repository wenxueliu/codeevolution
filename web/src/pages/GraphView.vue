<template>
  <div class="graph-view-page">
    <template v-if="!viewId">
      <div class="page-header">
        <div><h1>{{ t('Graph View') }}</h1><p>{{ t('选择一个已创建的 View，查看固定快照集合的跨仓拓扑。') }}</p></div>
        <router-link class="secondary" :to="{ name: 'snapshots' }">{{ t('管理 Snapshots') }}</router-link>
      </div>
      <UiState v-if="catalogError" kind="error" :title="t('Graph View 目录加载失败')" :message="catalogError.message" :action-label="t('重试')" @action="loadCatalog" />
      <UiState v-else-if="catalogLoading" kind="loading" :title="t('正在加载 Graph View 目录')" />
      <section v-else-if="viewCatalog.length" class="view-list" data-testid="graph-views">
        <el-card v-for="view in viewCatalog" :key="view.id" class="view-card" shadow="hover" data-testid="graph-view-card">
          <div>
            <h2>{{ view.label || t('未命名 View') }}</h2>
            <p>{{ view.lifecycle === 'pinned' ? t('已固定') : t('临时 View') }} · {{ view.completeness === 'complete' ? t('快照完整') : t('包含不可用成员') }}</p>
            <p>{{ t('{count} 个成员：{names}', { count: view.members?.length || 0, names: memberNames(view) }) }}</p>
            <code>{{ view.id }}</code>
          </div>
          <el-button type="primary" @click="$router.push({ name: 'graph-view', params: { viewId: view.id } })">{{ t('查看 View') }}</el-button>
        </el-card>
      </section>
      <UiState v-else kind="empty" :title="t('还没有 Graph View')" :message="t('请先在 Snapshots 中选择成员并创建当前 View。')">
        <router-link class="primary" :to="{ name: 'snapshots' }">{{ t('前往 Snapshots') }}</router-link>
      </UiState>
    </template>

    <template v-else>
      <div class="page-header">
        <div><h1>{{ t('Graph View') }}</h1><p>{{ t('以固定的快照集合浏览跨仓服务拓扑、变更影响和调用流程。') }}</p></div>
        <div class="page-actions"><router-link class="secondary" :to="{ name: 'terms', query: { view_id: viewId } }">{{ t('跨服务术语对齐') }}</router-link><router-link class="secondary" :to="{ name: 'snapshots' }">{{ t('返回 Snapshots') }}</router-link></div>
      </div>
      <UiState v-if="error" kind="error" :title="t('Graph View 加载失败')" :message="error.message" :action-label="t('重试')" @action="load" />
      <UiState v-else-if="loading" kind="loading" :title="t('正在加载 Graph View')" />
      <template v-else>
        <el-card class="view-meta" shadow="never">
          <el-descriptions :column="4" border>
            <el-descriptions-item :label="t('View ID')"><code>{{ viewId }}</code></el-descriptions-item>
            <el-descriptions-item :label="t('Artifact')"><el-tag :type="artifact ? 'success' : 'info'">{{ artifact ? t('已生成') : t('未生成') }}</el-tag></el-descriptions-item>
            <el-descriptions-item :label="t('服务')"><b>{{ services.length || '—' }}</b></el-descriptions-item>
            <el-descriptions-item :label="t('调用边')"><b>{{ serviceEdges.length || '—' }}</b></el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card v-if="!artifact" class="panel artifact-empty" shadow="never">
          <el-result icon="info" :title="t('尚未生成拓扑 Artifact')" :sub-title="t('Graph View 只固定 Scope 和 Snapshot；拓扑分析需要显式创建一次 Artifact Job。')">
            <template #extra><el-button type="primary" :loading="artifactLoading" @click="generate">{{ t('生成拓扑') }}</el-button></template>
          </el-result>
          <el-alert v-if="jobStatus" :title="`Job ${jobStatus.id}：${jobStatus.status}`" type="info" :closable="false" />
        </el-card>
        <UiState v-else-if="!services.length" kind="empty" :title="t('此 View 没有可浏览的服务')" :message="t('当前 Artifact 没有可分析的 Scope 成员。')" />
        <template v-else>
          <el-card class="panel" shadow="never">
            <template #header><div class="panel-heading"><div><h2>{{ t('服务拓扑') }}</h2><p class="muted">{{ t('边仅来自此 View 固定快照中的通信事实；点击服务查看影响和流程。') }}</p></div><el-tag>{{ t('{count} 个服务', { count: services.length }) }}</el-tag></div></template>
            <el-space wrap class="service-list" :aria-label="t('服务列表')">
              <el-button v-for="service in services" :key="service.member_id" :type="selectedService === service.member_id ? 'primary' : 'default'" @click="selectService(service.member_id)">
                {{ service.display_name || service.member_id }} <small>{{ service.availability || `${service.entries?.length || 0} ${t('entries')}` }}</small>
              </el-button>
            </el-space>
            <TopologyGraph :graph="serviceGraph" :aria-label="t('服务拓扑图')" :empty-text="t('未在当前快照中发现跨服务调用边。')" />
            <el-divider />
            <el-table v-if="serviceEdges.length" :data="serviceEdges" size="small" stripe>
              <el-table-column :label="t('调用方')" min-width="150"><template #default="{ row }">{{ serviceName(row.source_member_id) }}</template></el-table-column>
              <el-table-column label="→" width="55" align="center" />
              <el-table-column :label="t('目标服务')" min-width="150"><template #default="{ row }">{{ targetName(row) }}</template></el-table-column>
              <el-table-column :label="t('类型')" width="110"><template #default="{ row }"><el-tag size="small">{{ row.kind }}</el-tag></template></el-table-column>
              <el-table-column :label="t('置信度')" width="120"><template #default="{ row }"><el-tag v-if="row.confidence" size="small" type="success">{{ confidenceText(row.confidence) }}</el-tag><span v-else>—</span></template></el-table-column>
              <el-table-column :label="t('证据')" min-width="130"><template #default="{ row }"><el-collapse v-if="row.evidence || row.dependency" accordion><el-collapse-item :title="row.evidence ? t('调用证据') : t('依赖证据')" name="evidence"><pre>{{ formatJson(row.evidence || row.dependency) }}</pre></el-collapse-item></el-collapse><span v-else>—</span></template></el-table-column>
            </el-table>
            <el-empty v-else :description="t('未在当前快照中发现跨服务调用边。')" :image-size="70" />
          </el-card>

          <section class="visual-grid">
            <el-card class="panel" shadow="never">
              <template #header><div class="panel-heading"><h2>{{ t('覆盖情况') }}</h2><el-tag :type="coverageTagType(artifact.coverage?.status)">{{ statusText(artifact.coverage?.status) }}</el-tag></div></template>
              <div v-if="coverageRows.length" class="coverage-list">
                <div v-for="row in coverageRows" :key="row.member_id" class="coverage-row">
                  <div class="coverage-label"><b>{{ serviceName(row.member_id) }}</b><el-tag size="small" :type="coverageTagType(row.status)">{{ statusText(row.status) }}</el-tag></div>
                  <el-progress :percentage="coveragePercent(row.status)" :status="row.status === 'complete' ? 'success' : undefined" :stroke-width="10" />
                  <div class="collector-list"><el-tag v-for="collector in row.collectors" :key="`${row.member_id}-${collector.collector}`" size="small" :type="collectorTagType(collector.status)">{{ collector.collector }} · {{ statusText(collector.status) }}</el-tag></div>
                  <p v-if="row.reason" class="muted">{{ row.reason }}</p>
                </div>
              </div>
              <el-empty v-else :description="t('暂无覆盖数据')" :image-size="60" />
            </el-card>

            <el-card class="panel" shadow="never">
              <template #header><div class="panel-heading"><h2>{{ t('资源访问图') }}</h2><el-tag type="warning">{{ t('{count} 个资源', { count: resources.length }) }}</el-tag></div></template>
              <TopologyGraph :graph="resourceGraph" :aria-label="t('资源访问图')" :empty-text="t('未发现资源访问')" />
              <el-table v-if="resources.length" :data="resources" size="small" class="resource-table">
                <el-table-column :label="t('服务')" min-width="120"><template #default="{ row }">{{ serviceName(row.source_member_id) }}</template></el-table-column>
                <el-table-column :label="t('资源')" min-width="170"><template #default="{ row }">{{ resourceLabel(row) }}</template></el-table-column>
                <el-table-column :label="t('操作')" width="100"><template #default="{ row }">{{ row.resource?.operation || '—' }}</template></el-table-column>
              </el-table>
            </el-card>
          </section>

          <el-card v-if="artifact.candidates?.length || artifact.boundary_dependencies?.length" class="panel" shadow="never">
            <template #header><h2>{{ t('待确认与边界') }}</h2></template>
            <el-alert v-if="artifact.candidates?.length" :title="t('候选/未解析边（{count}）', { count: artifact.candidates.length })" type="warning" :closable="false" />
            <el-alert v-if="artifact.boundary_dependencies?.length" :title="t('Scope 外边界（{count}）', { count: artifact.boundary_dependencies.length })" type="info" :closable="false" />
            <el-table :data="[...(artifact.candidates || []), ...(artifact.boundary_dependencies || [])]" size="small" class="compact-table">
              <el-table-column :label="t('类型')" width="130"><template #default="{ row }"><el-tag size="small" :type="row.kind === 'known_external' ? 'info' : 'warning'">{{ row.kind }}</el-tag></template></el-table-column>
              <el-table-column :label="t('服务')" min-width="150"><template #default="{ row }">{{ serviceName(row.source_member_id || row.source?.member_id) }}</template></el-table-column>
              <el-table-column :label="t('原因')" min-width="260"><template #default="{ row }">{{ row.reason || '—' }}</template></el-table-column>
            </el-table>
          </el-card>

          <el-card class="panel analysis-panel" shadow="never">
            <template #header><div class="analysis-heading"><div><h2>{{ t('影响与流程') }}</h2><p class="muted">{{ t('选择服务后，以同一 view_id 查询。') }}</p></div><el-form inline @submit.prevent="loadAnalysis"><el-form-item :label="t('起始 API（可选）')"><el-input v-model="method" placeholder="GET" class="method-input" @change="loadAnalysis" /><el-input v-model="path" placeholder="/api/orders" class="path-input" @change="loadAnalysis" /></el-form-item></el-form></div></template>
            <UiState v-if="analysisError" kind="error" :title="t('分析加载失败')" :message="analysisError.message" :action-label="t('重试')" @action="loadAnalysis" />
            <el-skeleton v-else-if="analysisLoading" :rows="4" animated />
            <template v-else>
              <TopologyGraph :graph="impactGraph" :aria-label="t('影响关系图')" :empty-text="t('没有确认的服务依赖。')" />
              <div class="analysis-columns">
                <el-card shadow="never"><template #header><h3>{{ selectedService }}{{ t('的下游依赖') }}</h3></template><el-tag v-for="item in impact.downstream_dependencies || []" :key="item.member_id" class="path-tag" type="success">{{ serviceName(item.member_id) }} <small>{{ item.path?.join(' → ') }}</small></el-tag><el-empty v-if="!(impact.downstream_dependencies || []).length" :description="t('没有确认的下游服务。')" :image-size="55" /></el-card>
                <el-card shadow="never"><template #header><h3>{{ t('上游依赖方') }}</h3></template><el-tag v-for="item in impact.upstream_dependents || []" :key="item.member_id" class="path-tag" type="primary">{{ serviceName(item.member_id) }} <small>{{ item.path?.slice().reverse().join(' → ') }}</small></el-tag><el-empty v-if="!(impact.upstream_dependents || []).length" :description="impact.upstream_status ? t('上游关系可能受覆盖范围影响。') : t('没有确认的上游服务。')" :image-size="55" /></el-card>
              </div>
              <el-alert v-if="impact.upstream_status" class="analysis-warning" :title="t('上游关系可能受覆盖范围影响。')" type="warning" :closable="false" />
              <el-card v-if="flow.nodes?.length" shadow="never" class="flow-card"><template #header><div class="panel-heading"><h3>{{ t('静态流程图') }}</h3><el-tag>{{ t('{count} 个节点', { count: flow.nodes.length }) }}</el-tag></div></template><TopologyGraph :graph="flowGraph" :aria-label="t('静态流程图')" :empty-text="t('暂无流程节点')" /></el-card>
            </template>
          </el-card>
        </template>
      </template>
    </template>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'
import TopologyGraph from '../components/TopologyGraph.vue'
import { t } from '../i18n.js'

export default {
  components: { UiState, TopologyGraph },
  props: { viewId: { type: String, default: '' } },
  data() { return { viewCatalog: [], catalogLoading: false, catalogError: null, artifact: null, selectedService: '', impact: {}, flow: {}, method: 'GET', path: '', loading: false, artifactLoading: false, analysisLoading: false, error: null, analysisError: null, jobStatus: null, pollTimer: null } },
  computed: {
    services() { return this.artifact?.services || [] },
    serviceEdges() {
      const byKey = new Map()
      for (const item of [...(this.artifact?.service_projections || []), ...(this.artifact?.endpoint_dependencies || []), ...(this.artifact?.edges || [])]) {
        const source = item.source_member_id || item.source?.member_id
        const target = item.target_member_id || item.target?.member_id
        if (!source || !target || source === target || !['http', 'message', 'grpc'].includes(item.kind)) continue
        const key = `${source}:${target}:${item.kind}`
        if (!byKey.has(key)) byKey.set(key, { ...item, source_member_id: source, target_member_id: target })
      }
      return [...byKey.values()]
    },
    resources() { return this.artifact?.resource_dependencies || [] },
    coverageRows() {
      const coverage = this.artifact?.coverage || {}
      const members = coverage.members || Object.fromEntries(Object.entries(coverage.member_details || {}).map(([id, detail]) => [id, detail.status]))
      return Object.entries(members).map(([member_id, status]) => ({ member_id, status, ...(coverage.member_details?.[member_id] || {}) }))
    },
    serviceGraph() {
      return this.layoutGraph(this.services.map((item) => ({ id: item.member_id, label: item.display_name || item.member_id, meta: `${item.entries?.length || 0} ${this.t('entries')}` })), this.serviceEdges)
    },
    resourceGraph() {
      const resourceMembers = new Set(this.resources.map((item) => item.source_member_id))
      const nodes = this.services.filter((item) => resourceMembers.has(item.member_id)).map((item) => ({ id: `service:${item.member_id}`, label: item.display_name || item.member_id, meta: this.t('服务') }))
      const edges = []
      this.resources.forEach((item, index) => {
        const id = `resource:${item.edge_id || index}`
        nodes.push({ id, label: this.resourceLabel(item), meta: item.resource?.operation || this.t('资源'), kind: 'resource' })
        edges.push({ source: `service:${item.source_member_id}`, target: id, kind: 'resource' })
      })
      return this.layoutGraph(nodes, edges)
    },
    impactGraph() {
      const nodes = [{ id: this.selectedService, label: this.serviceName(this.selectedService), meta: this.t('当前服务') }]
      const edges = []
      for (const item of this.impact.downstream_dependencies || []) { nodes.push({ id: item.member_id, label: this.serviceName(item.member_id), meta: this.t('下游') }); edges.push(...this.pathEdges(item.path, false)) }
      for (const item of this.impact.upstream_dependents || []) { nodes.push({ id: item.member_id, label: this.serviceName(item.member_id), meta: this.t('上游') }); edges.push(...this.pathEdges(item.path, true)) }
      return this.layoutGraph(nodes, edges)
    },
    flowGraph() {
      const nodes = (this.flow.nodes || []).map((item) => ({ id: item.node_id || `${item.member_id}:${item.entry_id}`, label: this.serviceName(item.member_id), meta: item.entry_id || item.kind || '', kind: item.kind === 'resource' ? 'resource' : 'service' }))
      let edges = (this.flow.edges || []).map((item) => ({ source: item.source?.node_id || `${item.source?.member_id}:${item.source?.entry_id}`, target: item.target?.node_id || `${item.target?.member_id}:${item.target?.entry_id}`, kind: item.kind }))
      if (!edges.length && nodes.length > 1) edges = nodes.slice(1).map((item, index) => ({ source: nodes[index].id, target: item.id, kind: 'http' }))
      return this.layoutGraph(nodes, edges)
    },
  },
  watch: { async viewId(value) { if (value) await this.load(); else await this.loadCatalog() } },
  async created() { if (this.viewId) await this.load(); else await this.loadCatalog() },
  beforeUnmount() { this.stopPolling() },
  methods: {
    t,
    async loadCatalog() { this.catalogLoading = true; this.catalogError = null; try { const response = await this.$api.get('/api/graph-views'); this.viewCatalog = response.views || [] } catch (error) { this.catalogError = error } finally { this.catalogLoading = false } },
    memberNames(view) { return (view.members || []).map((member) => member.display_name || member.member_id).join('、') || this.t('无成员') },
    async load() { if (!this.viewId) { this.loading = false; this.error = null; return }; this.loading = true; this.error = null; try { const response = await this.$api.get(`/api/graph-views/${this.viewId}/artifacts/topology`); this.artifact = response.artifact; const first = this.services[0]?.member_id; if (first) await this.selectService(first) } catch (error) { if (error.status === 404 && (error.body?.detail === 'artifact_not_generated' || error.body?.error?.code === 'artifact_not_generated')) this.artifact = null; else this.error = error } finally { this.loading = false } },
    async generate() { this.artifactLoading = true; this.error = null; try { const response = await this.$api.request(`/api/graph-views/${this.viewId}/artifact-jobs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artifact_kind: 'topology', params: {} }) }); this.jobStatus = response.job; if (response.job?.status === 'completed') await this.load(); else this.startPolling(response.job?.id) } catch (error) { this.error = error } finally { this.artifactLoading = false } },
    startPolling(jobId) { this.stopPolling(); if (!jobId) return; const poll = async () => { try { const response = await this.$api.get(`/api/graph-artifact-jobs/${jobId}`); this.jobStatus = response.job; if (['completed', 'failed', 'cancelled', 'interrupted'].includes(response.job?.status)) { this.stopPolling(); if (response.job.status === 'completed') await this.load(); else this.error = new Error(response.job.error_message || `Artifact Job ${response.job.status}`) } } catch (error) { this.stopPolling(); this.error = error } }; this.pollTimer = window.setInterval(poll, 2000); poll() },
    stopPolling() { if (this.pollTimer) { window.clearInterval(this.pollTimer); this.pollTimer = null } },
    async selectService(service) { this.selectedService = service; await this.loadAnalysis() },
    async loadAnalysis() { if (!this.selectedService) return; this.analysisLoading = true; this.analysisError = null; try { const query = { member_id: this.selectedService }; this.impact = await this.$api.get(`/api/graph-views/${this.viewId}/impact`, query); this.flow = this.path ? await this.$api.get(`/api/graph-views/${this.viewId}/flow`, { ...query, method: this.method || 'GET', path: this.path }) : {} } catch (error) { this.analysisError = error } finally { this.analysisLoading = false } },
    serviceName(id) { return this.services.find((item) => item.member_id === id)?.display_name || id || '—' },
    targetName(edge) { return edge.target_member_id ? this.serviceName(edge.target_member_id) : (edge.target?.member_id ? this.serviceName(edge.target.member_id) : (edge.target_candidates || []).join(', ') || this.t('外部依赖')) },
    resourceLabel(item) { const resource = item.resource || {}; return resource.instance_id || resource.resource_id || resource.name || resource.type || item.observation_id || this.t('未知资源') },
    confidenceText(value) { return typeof value === 'object' ? value.level || value.score || '—' : value },
    statusText(value) { return this.t(({ complete: '完整', partial: '部分', available: '可用', unavailable: '不可用', unsupported: '不支持', failed: '失败' })[value] || value || '未知') },
    coverageTagType(value) { return value === 'complete' || value === 'available' ? 'success' : value === 'partial' ? 'warning' : 'info' },
    collectorTagType(value) { return value === 'complete' ? 'success' : value === 'unsupported' ? 'info' : 'warning' },
    coveragePercent(value) { return value === 'complete' || value === 'available' ? 100 : value === 'partial' ? 55 : 0 },
    pathEdges(path, reverse) { if (!path || path.length < 2) return []; return path.slice(0, -1).map((_, index) => ({ source: reverse ? path[index + 1] : path[index], target: reverse ? path[index] : path[index + 1], kind: 'http' })) },
    layoutGraph(rawNodes, rawEdges) {
      const unique = [...new Map(rawNodes.filter((item) => item?.id).map((item) => [item.id, item])).values()]
      const columns = Math.min(4, Math.max(1, unique.length)); const width = Math.max(760, columns * 205 + 50)
      const nodes = unique.map((item, index) => ({ ...item, x: 25 + (index % columns) * 205, y: 25 + Math.floor(index / columns) * 92, width: 175, height: 58 }))
      const byId = new Map(nodes.map((item) => [item.id, item]))
      const edges = rawEdges.map((item, index) => { const source = byId.get(item.source); const target = byId.get(item.target); if (!source || !target) return null; const rightward = target.x >= source.x; return { ...item, id: item.id || `${item.source}-${item.target}-${index}`, x1: source.x + (rightward ? source.width : 0), y1: source.y + source.height / 2, x2: target.x + (rightward ? 0 : target.width), y2: target.y + target.height / 2 } }).filter(Boolean)
      return { nodes, edges, width, height: Math.max(150, Math.ceil(unique.length / columns) * 92 + 20) }
    },
    formatJson(value) { return JSON.stringify(value, null, 2) },
  },
}
</script>

<style scoped>
.view-meta { margin-bottom: 20px; }.panel { margin-bottom: 20px; }.panel h2, .panel h3 { margin: 0; }.panel-heading, .analysis-heading { display: flex; justify-content: space-between; gap: 20px; align-items: center; }.panel-heading p { margin: 5px 0 0; }.service-list { margin: 0 0 16px; }.service-list small { margin-left: 5px; opacity: .75; }.edge-card pre, pre { overflow: auto; max-height: 180px; padding: 10px; background: #f6f8fa; border-radius: 6px; font-size: 12px; }
.page-actions { display:flex; gap:10px; align-items:center; }
.visual-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }.coverage-list { display: grid; gap: 18px; }.coverage-label { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }.collector-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }.coverage-row p { margin: 5px 0 0; font-size: 12px; }.resource-table { margin-top: 14px; }.compact-table { margin-top: 12px; }.analysis-panel { overflow: hidden; }.analysis-heading { align-items: start; }.analysis-heading h2 { margin-bottom: 4px; }.analysis-heading .el-form { margin: 0; }.analysis-heading :deep(.el-form-item) { margin: 0; }.method-input { width: 82px; }.path-input { width: 190px; margin-left: 6px; }.analysis-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 18px; }.analysis-columns :deep(.el-card__header) { padding: 12px 14px; }.analysis-columns :deep(.el-card__body) { min-height: 90px; padding: 14px; }.path-tag { margin: 0 6px 6px 0; }.path-tag small { margin-left: 6px; opacity: .8; }.analysis-warning { margin-top: 14px; }.flow-card { margin-top: 18px; }.muted { color: #667085; }.view-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }.view-card :deep(.el-card__body) { display: flex; align-items: center; justify-content: space-between; gap: 16px; }.view-card h2 { margin: 0 0 6px; font-size: 18px; }.view-card p { margin: 0 0 6px; color: #667085; font-size: 13px; }.view-card code { overflow-wrap: anywhere; font-size: 11px; color: #666; }.artifact-empty { text-align: center; }.primary { border: 0; border-radius: 5px; padding: 9px 15px; color: #fff; background: #315d9b; cursor: pointer; }.secondary { color: #315d9b; text-decoration: none; }.entry-input { display: flex; gap: 6px; }
@media (max-width: 900px) { .visual-grid, .analysis-columns { grid-template-columns: 1fr; }.analysis-heading { display: block; }.analysis-heading .el-form { margin-top: 12px; } }
</style>
