<template>
  <div class="knowledge">
    <UiState v-if="error" kind="error" title="知识提取失败" :message="error.message" action-label="重试" dismiss-label="关闭" @action="load(false)" @dismiss="error = null" />
    <UiState v-if="loading" kind="loading" title="正在从 CodeGraph 提取结构知识" message="大型仓库可能需要等待片刻，完成前可以继续浏览当前结果。" />

    <div class="page-header">
      <div>
        <h1>知识中心</h1>
        <p>基于当前 CodeGraph 索引实时推导，结果不写入演进数据库。<span v-if="loadedAt"> 最近刷新：{{ loadedAt }} · {{ loadDuration }} ms</span></p>
      </div>
      <div class="actions">
        <button class="secondary" :disabled="loading" @click="load(false)">刷新结构知识</button>
        <button class="primary" :disabled="loading" @click="loadLlm">
          {{ llmLoaded ? '重新抽取 LLM 知识' : '抽取 LLM 知识' }}
        </button>
      </div>
    </div>

    <div class="notice" v-if="!llmLoaded">
      业务描述、业务规则、错误目录和状态机需要 LLM，可通过页面顶部“LLM 设置”配置，仅在点击抽取时调用。
    </div>

    <div class="summary-grid" v-if="report">
      <button
        v-for="item in summaryCards"
        :key="item.key"
        class="summary-card"
        :class="{ active: activeSection === item.key }"
        @click="activeSection = item.key"
      >
        <span class="summary-value">{{ item.value }}</span>
        <span class="summary-label">{{ item.label }}</span>
      </button>
    </div>

    <div class="content" v-if="report">
      <aside class="section-nav">
        <button
          v-for="section in sections"
          :key="section.key"
          :class="{ active: activeSection === section.key, semantic: section.llm }"
          @click="activeSection = section.key"
        >
          <span>{{ section.label }}</span><small>{{ section.phase }}</small>
        </button>
      </aside>

      <section class="panel">
        <div class="panel-title">
          <div><h2>{{ activeMeta.label }}</h2><p>{{ activeMeta.description }}</p></div>
          <span class="phase">{{ activeMeta.phase }}</span>
        </div>

        <template v-if="activeSection === 'api_contract'">
          <div class="metric">{{ activeData.endpoint_count || 0 }} <small>个端点</small></div>
          <div class="table-tools">
            <label>筛选端点<input v-model.trim="endpointSearch" type="search" placeholder="路径、处理函数或仓库" @input="endpointPage = 1" /></label>
            <label>HTTP 方法<select v-model="endpointMethod" @change="endpointPage = 1"><option value="">全部方法</option><option v-for="method in endpointMethods" :key="method">{{ method }}</option></select></label>
            <span>共 {{ filteredEndpoints.length }} 条</span>
          </div>
          <div class="table-wrap"><table><thead><tr><th>方法</th><th>路径</th><th>处理函数</th><th>请求/应答</th><th>前端调用</th></tr></thead><tbody>
            <template v-for="(item, index) in visibleEndpoints" :key="`${item.repository || ''}-${item.method}-${item.path}-${index}`">
              <tr class="clickable" :class="{ expanded: expandedKeys.has(endpointKey(item, index)) }" tabindex="0" @click="toggleEndpoint(item, index)" @keydown.enter="toggleEndpoint(item, index)">
                <td><span class="method">{{ item.method }}</span></td><td><code>{{ item.path }}</code></td><td>{{ item.handler || '-' }}</td>
                <td>{{ item.request_body?.type || '无请求体' }} → {{ item.response_body?.type || item.return_type || '未知' }}</td>
                <td>{{ item.frontend_callers?.length || 0 }} 处</td>
              </tr>
              <tr v-if="expandedKeys.has(endpointKey(item, index))" class="expand-detail">
                <td colspan="5">
                  <div class="contract-grid">
                    <div><h4>请求头</h4><pre>{{ formatJson(item.request_headers || []) }}</pre></div>
                    <div><h4>路径/查询参数</h4><pre>{{ formatJson({ path: item.path_params || [], query: item.query_params || [] }) }}</pre></div>
                    <div><h4>请求体</h4><pre>{{ formatJson(item.request_body) }}</pre></div>
                    <div><h4>应答体</h4><pre>{{ formatJson(item.response_body) }}</pre></div>
                  </div>
                  <h4>后端调用链</h4>
                  <div v-if="item.call_chain_mermaid" class="mermaid-wrap">
                    <pre class="mermaid">{{ item.call_chain_mermaid }}</pre>
                  </div>
                  <div v-else class="call-chain"><span v-for="(node, idx) in item.call_chain || []" :key="node.id">{{ node.name }}<b v-if="idx < item.call_chain.length - 1">→</b></span></div>
                  <h4>前端调用位置</h4>
                  <div class="frontend-call" v-for="call in item.frontend_callers || []" :key="call.definition_file + call.function">
                    <b>{{ call.function }}</b> · <code>{{ call.definition_file }}:{{ call.definition_line }}</code>
                    <div v-for="site in call.call_sites" :key="site.file + site.line"><code>{{ site.file }}:{{ site.line }}</code></div>
                  </div>
                  <p class="muted" v-if="!item.frontend_callers?.length">未匹配到前端调用。</p>
                </td>
              </tr>
            </template>
          </tbody></table></div>
          <div class="pagination" v-if="endpointPages > 1">
            <button :disabled="endpointPage === 1" @click="endpointPage--">上一页</button>
            <span>第 {{ endpointPage }} / {{ endpointPages }} 页</span>
            <button :disabled="endpointPage === endpointPages" @click="endpointPage++">下一页</button>
          </div>
        </template>

        <template v-else-if="activeSection === 'module_topology'">
          <div class="metrics"><div class="metric">{{ activeData.module_count || 0 }} <small>个模块</small></div><div class="metric">{{ activeData.coupling_score ?? '-' }} <small>耦合度</small></div></div>
          <div class="card-grid"><div class="detail-card" v-for="item in activeData.modules || []" :key="item.id"><h3>{{ item.name }}</h3><p>{{ item.file_count }} 个文件 · {{ item.primary_language || '未知语言' }}</p><code>{{ item.id }}</code></div></div>
        </template>

        <template v-else-if="activeSection === 'core_entities'">
          <div class="table-wrap"><table><thead><tr><th>领域对象</th><th>类型</th><th>字段</th><th>关系</th><th>领域分</th><th>仓库/文件</th></tr></thead><tbody>
            <template v-for="item in activeData || []" :key="item.qualified_name">
              <tr class="clickable" :class="{ expanded: expandedEntityKeys.has(item.node_id || item.qualified_name) }" tabindex="0" @click="toggleEntity(item)" @keydown.enter="toggleEntity(item)">
                <td><b>{{ item.name }}</b></td><td>{{ item.kind }}</td><td>{{ item.field_count }}</td><td>{{ item.relationship_count }}</td><td>{{ Number(item.score || 0).toFixed(2) }}</td><td><span class="repo-badge">{{ item.repository }}</span><code>{{ item.file_path }}</code></td>
              </tr>
              <tr v-if="expandedEntityKeys.has(item.node_id || item.qualified_name)" class="expand-detail">
                <td colspan="6">
                  <div class="entity-detail">
                    <div><strong>限定名:</strong> <code>{{ item.qualified_name }}</code></div>
                    <div><strong>类型:</strong> {{ item.kind }} &middot; <strong>分层:</strong> {{ item.layer || '未分类' }} &middot; <strong>领域分:</strong> {{ Number(item.score || 0).toFixed(2) }}</div>
                    <div><strong>文件位置:</strong> <code>{{ item.file_path }}{{ item.start_line ? ':' + item.start_line : '' }}</code></div>
                    <div v-if="item.annotations?.length"><strong>标注:</strong> {{ item.annotations.join(', ') }}</div>
                    <div v-if="item.fields?.length" class="field-list">
                      <strong>字段 ({{ item.fields.length }}):</strong>
                      <table class="field-table"><thead><tr><th>名称</th><th>类型</th><th>行</th></tr></thead><tbody>
                        <tr v-for="f in item.fields" :key="f.name"><td><code>{{ f.name }}</code></td><td>{{ f.signature || f.kind || '-' }}</td><td>{{ f.start_line || '-' }}</td></tr>
                      </tbody></table>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'test_coverage'">
          <div class="metrics"><div class="metric">{{ activeData.coverage_pct ?? 0 }}% <small>覆盖率</small></div><div class="metric">{{ activeData.gap_count || 0 }} <small>个测试缺口</small></div></div>
          <div class="table-wrap"><table><thead><tr><th>未覆盖符号</th><th>类型</th><th>位置</th></tr></thead><tbody><tr v-for="item in activeData.top_gaps || []" :key="item.qualified_name"><td>{{ item.qualified_name }}</td><td>{{ item.kind }}</td><td><code>{{ item.file_path }}:{{ item.line }}</code></td></tr></tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'layer_violations'">
          <div class="metric danger">{{ activeData.violation_count || 0 }} <small>个分层违规</small></div>
          <div class="table-wrap"><table><thead><tr><th>来源</th><th>依赖</th><th>目标</th></tr></thead><tbody><tr v-for="(item, index) in activeData.violations || []" :key="index"><td><b>{{ item.source_layer }}</b><br><code>{{ item.source_file }}</code></td><td>→</td><td><b>{{ item.target_layer }}</b><br><code>{{ item.target_file }}</code></td></tr></tbody></table></div>
        </template>

        <template v-else>
          <div v-if="isDisabled(activeData)" class="empty-semantic">
            <p>{{ activeData.note }}</p><button class="primary" @click="loadLlm">现在抽取</button>
          </div>
          <pre v-else class="json-view">{{ formatJson(activeData) }}</pre>
        </template>
      </section>
    </div>

    <div v-else-if="!loading && !error" class="empty-state">暂无知识数据</div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'

const SECTIONS = [
  ['api_contract', 'API 契约', 'Phase 1', '路由、方法、处理函数与参数'],
  ['module_topology', '模块拓扑', 'Phase 1', '模块聚类、依赖关系与耦合度'],
  ['core_entities', '核心实体', 'Phase 1', '按字段、类型关系与领域语义识别核心领域对象'],
  ['test_coverage', '测试缺口', 'Phase 1', '生产函数覆盖率与未覆盖列表'],
  ['layer_violations', '分层违规', 'Phase 1', '跨层依赖和架构边界违规'],
  ['config_consumption', '配置消费', 'Phase 2', '配置键与代码消费者的对应关系'],
  ['external_dependencies', '外部依赖', 'Phase 2', '外部服务、库与中间件分类'],
  ['authorization_model', '权限模型', 'Phase 2', '受保护端点、角色和权限'],
  ['heat_map', '代码热力图', 'Phase 2', '按调用关系识别热点和冷点函数'],
  ['business_descriptions', '业务描述', 'Phase 3 · LLM', '核心函数的业务语义摘要', true],
  ['business_rules', '业务规则', 'Phase 3 · LLM', '验证、转换、授权与工作流规则', true],
  ['error_catalog', '错误目录', 'Phase 3 · LLM', '错误类型、触发条件与处理策略', true],
  ['state_machines', '状态机', 'Phase 3 · LLM', '状态、转换和触发器', true],
].map(([key, label, phase, description, llm = false]) => ({ key, label, phase, description, llm }))

let mermaidPromise

function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'neutral',
        securityLevel: 'loose',
        fontFamily: 'system-ui, sans-serif',
      })
      return mermaid
    })
  }
  return mermaidPromise
}

export default {
  components: { UiState },
  props: { repoName: String },
  data() { return { report: null, activeSection: 'api_contract', llmLoaded: false, sections: SECTIONS, expandedKeys: new Set(), expandedEntityKeys: new Set(), endpointSearch: '', endpointMethod: '', endpointPage: 1, endpointPageSize: 25, loadedAt: '', loadDuration: 0 } },
  computed: {
    activeMeta() { return this.sections.find(item => item.key === this.activeSection) || this.sections[0] },
    activeData() { return this.report?.[this.activeSection] ?? {} },
    summaryCards() {
      return [
        { key: 'api_contract', label: 'API 端点', value: this.report.api_contract?.endpoint_count ?? 0 },
        { key: 'module_topology', label: '模块', value: this.report.module_topology?.module_count ?? 0 },
        { key: 'core_entities', label: '核心实体', value: this.report.core_entities?.length ?? 0 },
        { key: 'test_coverage', label: '测试覆盖率', value: `${this.report.test_coverage?.coverage_pct ?? 0}%` },
        { key: 'layer_violations', label: '分层违规', value: this.report.layer_violations?.violation_count ?? 0 },
      ]
    },
    filteredEndpoints() {
      const query = this.endpointSearch.toLowerCase()
      return (this.report?.api_contract?.endpoints || []).filter(item => {
        if (this.endpointMethod && item.method !== this.endpointMethod) return false
        if (!query) return true
        return [item.path, item.handler, item.repository].some(value => String(value || '').toLowerCase().includes(query))
      })
    },
    visibleEndpoints() { return this.filteredEndpoints.slice((this.endpointPage - 1) * this.endpointPageSize, this.endpointPage * this.endpointPageSize) },
    endpointPages() { return Math.max(1, Math.ceil(this.filteredEndpoints.length / this.endpointPageSize)) },
    endpointMethods() { return [...new Set((this.report?.api_contract?.endpoints || []).map(item => item.method).filter(Boolean))].sort() },
  },
  async created() { await this.load(false) },
  methods: {
    async load(includeLlm) {
      const started = performance.now()
      await this.$runAsync(async () => {
        this.report = await this.$api.get('/api/knowledge', { repo: this.repoName || '', include_llm: includeLlm })
        this.llmLoaded = includeLlm
        this.endpointPage = 1
        this.expandedKeys = new Set()
        this.expandedEntityKeys = new Set()
        this.loadedAt = new Date().toLocaleTimeString()
        this.loadDuration = Math.round(performance.now() - started)
      })
    },
    async loadLlm() {
      if (!window.confirm('LLM 知识抽取可能需要较长时间并产生 API 调用费用，是否继续？')) return
      await this.load(true)
    },
    isDisabled(value) { return Boolean(value && !Array.isArray(value) && value.note) },
    formatJson(value) { return JSON.stringify(value, null, 2) },
    endpointKey(item, index) { return `${item.method || ''}-${item.path || ''}-${index}` },
    toggleEntity(item) {
      const key = item.node_id || item.qualified_name
      if (this.expandedEntityKeys.has(key)) {
        this.expandedEntityKeys.delete(key)
      } else {
        this.expandedEntityKeys.add(key)
      }
      this.expandedEntityKeys = new Set(this.expandedEntityKeys)
    },
    async toggleEndpoint(item, index) {
      const key = this.endpointKey(item, index)
      if (this.expandedKeys.has(key)) {
        this.expandedKeys.delete(key)
      } else {
        this.expandedKeys.add(key)
      }
      // trigger reactivity for Set
      this.expandedKeys = new Set(this.expandedKeys)
      if (this.expandedKeys.has(key) && item.call_chain_mermaid) {
        await this.$nextTick()
        setTimeout(() => this.renderMermaid(), 50)
      }
    },
    async renderMermaid() {
      const els = this.$el.querySelectorAll('.expand-detail .mermaid:not([data-processed])')
      if (!els.length) return
      try {
        const mermaid = await loadMermaid()
        for (const el of els) {
          await mermaid.run({ nodes: [el] })
        }
      } catch (_) { /* silently ignore mermaid render errors */ }
    },
  },
}
</script>

<style scoped>
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 16px; }
.page-header h1 { font-size: 24px; margin-bottom: 5px; }
.page-header p { color: #777; font-size: 13px; }
.actions { display: flex; gap: 8px; flex-shrink: 0; }
button { font: inherit; }
.actions button, .empty-semantic button { border: 0; border-radius: 6px; padding: 9px 14px; cursor: pointer; }
button:disabled { opacity: .55; cursor: wait; }
.primary { background: #e94560; color: white; }
.secondary { background: #ececf2; color: #333; }
.notice { background: #fff8e1; color: #795c12; border: 1px solid #ffe6a3; border-radius: 6px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px; }
.summary-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 18px; }
.summary-card { border: 1px solid #eee; background: white; border-radius: 8px; padding: 16px; text-align: left; cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,.05); }
.summary-card.active { border-color: #e94560; box-shadow: 0 0 0 1px #e94560; }
.summary-value, .summary-label { display: block; }
.summary-value { color: #e94560; font-size: 25px; font-weight: 700; }
.summary-label { color: #777; font-size: 12px; margin-top: 3px; }
.content { display: grid; grid-template-columns: 210px minmax(0, 1fr); gap: 16px; align-items: start; }
.section-nav { background: white; border-radius: 8px; padding: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.section-nav button { width: 100%; border: 0; background: transparent; border-radius: 5px; padding: 9px 10px; display: flex; justify-content: space-between; cursor: pointer; color: #444; }
.section-nav button:hover { background: #f7f7f9; }
.section-nav button.active { background: #fff0f2; color: #c82d48; font-weight: 600; }
.section-nav button.semantic { border-top: 1px solid #f1f1f1; }
.section-nav small { color: #aaa; font-size: 9px; }
.panel { min-width: 0; background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.panel-title { display: flex; justify-content: space-between; border-bottom: 1px solid #eee; padding-bottom: 14px; margin-bottom: 16px; }
.panel-title h2 { font-size: 19px; margin-bottom: 4px; }
.panel-title p { color: #888; font-size: 12px; }
.phase { color: #e94560; background: #fff0f2; border-radius: 12px; padding: 4px 9px; font-size: 10px; height: fit-content; }
.metrics { display: flex; gap: 14px; }
.metric { display: inline-block; color: #e94560; font-size: 27px; font-weight: 700; margin-bottom: 16px; }
.metric small { color: #888; font-size: 12px; font-weight: 400; }
.metric.danger { color: #c0392b; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }
.detail-card { border: 1px solid #eee; padding: 13px; border-radius: 6px; }
.detail-card h3 { font-size: 14px; margin-bottom: 5px; }
.detail-card p, .detail-card code { color: #888; font-size: 11px; }
.table-wrap { overflow-x: auto; }
.table-tools { display: flex; align-items: end; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.table-tools label { display: grid; gap: 4px; color: #666; font-size: 11px; }
.table-tools input, .table-tools select { min-width: 150px; border: 1px solid #cfd3da; border-radius: 5px; padding: 7px 8px; background: white; }
.table-tools > span { margin-left: auto; color: #888; font-size: 12px; }
.pagination { display: flex; justify-content: flex-end; align-items: center; gap: 10px; padding-top: 12px; font-size: 12px; color: #666; }
.pagination button { border: 1px solid #d7d9df; border-radius: 5px; padding: 6px 10px; background: white; cursor: pointer; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: #777; background: #f8f8fa; }
th, td { border-bottom: 1px solid #eee; padding: 9px 10px; vertical-align: top; }
td code { color: #666; word-break: break-all; }
.method { color: #e94560; font-weight: 700; }
.clickable { cursor: pointer; }
.clickable:hover { background: #fff8f9; }
.clickable.expanded { background: #fff0f2; }
.expand-detail td { padding: 0 10px 12px; border-bottom: 2px solid #e94560; background: #fffafb; }
.entity-detail { display: grid; gap: 6px; font-size: 12px; color: #444; }
.entity-detail strong { color: #333; }
.entity-detail code { color: #666; }
.field-list { margin-top: 4px; }
.field-table { margin-top: 4px; width: 100%; font-size: 11px; }
.field-table th { background: #f0f0f3; font-size: 10px; }
.field-table td { padding: 4px 8px; }
.contract-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; }
.contract-grid > div { min-width: 0; border: 1px solid #eee; border-radius: 6px; padding: 10px; background: #fff; }
.contract-grid h4, .api-detail > h4 { font-size: 12px; color: #555; margin-bottom: 7px; }
.contract-grid pre { margin: 0; max-height: 180px; overflow: auto; font-size: 10px; white-space: pre-wrap; }
.call-chain { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 15px; font-size: 11px; }
.call-chain span { background: #f1f1f5; padding: 4px 7px; border-radius: 4px; }
.call-chain b { color: #e94560; margin-left: 5px; }
.mermaid-wrap { margin-bottom: 15px; min-height: 60px; display: flex; justify-content: center; }
.mermaid-wrap :deep(svg) { max-width: 100%; height: auto; }
.frontend-call { border-left: 3px solid #e94560; padding: 5px 9px; margin: 6px 0; font-size: 11px; }
.muted { color: #aaa; font-size: 11px; }
.repo-badge { display: inline-block; margin-right: 5px; padding: 1px 5px; border-radius: 8px; background: #fff0f2; color: #c82d48; font-size: 9px; }
.json-view { max-height: 650px; overflow: auto; margin: 0; padding: 16px; border-radius: 6px; background: #171725; color: #d8d8e5; font-size: 11px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
.empty-semantic, .empty-state { text-align: center; padding: 60px 20px; color: #888; }
.empty-semantic p { margin-bottom: 14px; }
@media (max-width: 900px) {
  .page-header { display: block; } .actions { margin-top: 12px; }
  .summary-grid { grid-template-columns: repeat(2, 1fr); }
  .content { grid-template-columns: 1fr; }
  .section-nav { display: flex; overflow-x: auto; }
  .section-nav button { min-width: 145px; }
  .table-tools > span { margin-left: 0; width: 100%; }
}
</style>
