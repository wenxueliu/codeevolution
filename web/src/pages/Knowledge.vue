<template>
  <div class="knowledge">
    <UiState v-if="error" kind="error" title="知识提取失败" :message="error.message" action-label="重试" dismiss-label="关闭" @action="load(false)" @dismiss="error = null" />
    <UiState v-if="loading" kind="loading" title="正在从 CodeGraph 提取结构知识" message="大型仓库可能需要等待片刻，完成前可以继续浏览当前结果。" />

    <div class="page-header">
      <div>
        <h1>知识中心</h1>
        <p>基于当前 CodeGraph 索引实时推导。<span v-if="loadedAt"> 最近刷新：{{ loadedAt }} · {{ loadDuration }} ms</span></p>
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
            <label>服务<select v-model="endpointService" data-testid="endpoint-service-filter" @change="endpointPage = 1"><option value="">全部服务</option><option v-for="service in endpointServices" :key="service">{{ service }}</option></select></label>
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
                  <CallChainTree
                    v-if="item.handler && item.file && item.line"
                    :repo="repoName"
                    :member="item.repository"
                    :file="item.file"
                    :line="item.line"
                    :label="{ method: item.method, path: item.path, handler: item.handler }"
                    :mermaid="item.call_chain_mermaid"
                  />
                  <p class="muted" v-else>未解析到处理函数（file/line 缺失），无法展示调用链树。</p>
                  <details v-if="item.call_chain_mermaid" class="seq-details" @toggle="seqToggle($event, item)">
                    <summary>展开时序图</summary>
                    <div class="seq-actions">
                      <button class="secondary sm" type="button" data-testid="sequence-expand" @click.stop="openSequenceZoom(item)">放大查看</button>
                    </div>
                    <div class="mermaid-wrap"><pre class="mermaid">{{ item.call_chain_mermaid }}</pre></div>
                  </details>
                  <div v-if="!item.call_chain_mermaid && item.call_chain?.length" class="call-chain"><span v-for="(node, idx) in item.call_chain || []" :key="node.id || node.name">{{ node.name }}<b v-if="idx < item.call_chain.length - 1">→</b></span></div>
                  <h4>前端调用位置</h4>
                  <div class="frontend-call" v-for="call in item.frontend_callers || []" :key="call.definition_file + call.function">
                    <b>{{ call.function }}</b> · <code>{{ call.definition_file }}:{{ call.definition_line }}</code>
                    <div v-for="site in call.call_sites" :key="site.file + site.line"><code>{{ site.file }}:{{ site.line }}</code></div>
                  </div>
                  <p class="muted" v-if="!item.frontend_callers?.length">未匹配到前端调用。</p>
                  <section class="api-explanation-section" data-testid="api-explanation">
                    <div class="api-explanation-heading">
                      <div>
                        <h4>API 功能解释
                          <span v-if="explanationState(item).status" class="explanation-status" :class="'explanation-' + explanationState(item).status">{{ explanationStatusText(explanationState(item).status) }}</span>
                        </h4>
                        <p class="muted">解释由调用链叶子节点向入口聚合，仅在手动触发时调用模型。</p>
                      </div>
                      <div class="api-explanation-actions">
                        <button class="primary sm" type="button" data-testid="explanation-generate" :disabled="explanationState(item).running || !item.handler || !item.file || !item.line" @click.stop="generateEndpointExplanation(item)">{{ explanationState(item).current ? '手动刷新解释' : '生成 API 功能解释' }}</button>
                        <button class="secondary sm" type="button" @click.stop="toggleExplanationSnapshots(item)">{{ explanationState(item).showSnapshots ? '收起快照' : '管理快照' }}</button>
                      </div>
                    </div>
                    <p v-if="explanationState(item).loading" class="muted">正在读取解释快照…</p>
                    <p v-else-if="explanationState(item).error" class="explanation-error">{{ explanationState(item).error }}</p>
                    <p v-if="explanationState(item).running" class="explanation-progress">正在生成候选快照，当前解释仍可正常查看…</p>
                    <div v-if="explanationState(item).current" class="explanation-current">
                      <div class="explanation-meta">
                        <span>快照 {{ explanationState(item).current.id }}</span>
                        <span v-if="explanationState(item).current.source_revision">源码 {{ shortRevision(explanationState(item).current.source_revision) }}</span>
                        <span v-if="explanationState(item).current.model_id || explanationState(item).current.model">模型 {{ explanationState(item).current.model_id || explanationState(item).current.model }}</span>
                      </div>
                      <div v-if="snapshotExplanation(explanationState(item).current)" class="explanation-body">
                        <p class="explanation-summary">{{ snapshotExplanation(explanationState(item).current).summary || snapshotExplanation(explanationState(item).current).business_purpose_zh || snapshotExplanation(explanationState(item).current).business_purpose_en }}</p>
                        <details v-if="explanationSteps(explanationState(item).current).length"><summary>业务流程（{{ explanationSteps(explanationState(item).current).length }}）</summary><ol><li v-for="(step, stepIndex) in explanationSteps(explanationState(item).current)" :key="stepIndex">{{ explanationStepText(step) }}</li></ol></details>
                      </div>
                      <div class="coverage-grid" v-if="snapshotCoverage(explanationState(item).current)">
                        <span>节点 {{ coverageValue(explanationState(item).current, 'completed_nodes', 'translated_nodes') }}/{{ coverageValue(explanationState(item).current, 'total_nodes', 'nodes_total') }}</span>
                        <span>完整 {{ coveragePercent(explanationState(item).current) }}</span>
                        <span v-if="coverageValue(explanationState(item).current, 'partial_nodes')">部分 {{ coverageValue(explanationState(item).current, 'partial_nodes') }}</span>
                        <span v-if="coverageValue(explanationState(item).current, 'failed_nodes')">失败 {{ coverageValue(explanationState(item).current, 'failed_nodes') }}</span>
                      </div>
                      <details v-if="snapshotNodes(explanationState(item).current).length" class="node-explanations">
                        <summary>节点解释状态（{{ snapshotNodes(explanationState(item).current).length }}）</summary>
                        <article v-for="node in snapshotNodes(explanationState(item).current)" :key="node.node_key" class="node-explanation">
                          <header><code>{{ node.node_key }}</code><span class="explanation-status" :class="'explanation-' + node.status">{{ explanationStatusText(node.status) }}</span></header>
                          <p v-if="nodeSummary(node)">{{ nodeSummary(node) }}</p><small v-if="node.file">{{ node.file }}{{ node.line_start ? ':' + node.line_start : '' }}</small>
                        </article>
                      </details>
                    </div>
                    <p v-else-if="!explanationState(item).loading" class="muted">尚未生成该端点的解释快照。</p>
                    <div v-if="explanationState(item).showSnapshots" class="snapshot-list" data-testid="explanation-snapshots">
                      <h5>解释快照</h5><p v-if="!explanationState(item).snapshots.length" class="muted">暂无快照。</p>
                      <article v-for="snapshot in explanationState(item).snapshots" :key="snapshot.id" class="snapshot-item">
                        <div><b>{{ snapshot.id }}</b><span class="explanation-status" :class="'explanation-' + snapshot.status">{{ explanationStatusText(snapshot.status) }}</span><small>{{ formatSnapshotTime(snapshot.created_at) }}<template v-if="snapshot.model_id || snapshot.model"> · {{ snapshot.model_id || snapshot.model }}</template></small></div>
                        <button v-if="!['running', 'pending'].includes(snapshot.status)" class="secondary sm" type="button" :disabled="explanationState(item).deleting === snapshot.id" @click.stop="deleteExplanationSnapshot(item, snapshot)">删除</button>
                      </article>
                    </div>
                  </section>
                  <div class="business-rule-section">
                    <h4>业务规则 <span v-if="brState(item).status" class="br-status" :class="'br-' + brState(item).status">{{ brState(item).statusText }}</span></h4>
                    <div v-if="brState(item).editing" class="br-prompt-edit">
                      <textarea v-model="brState(item).editPrompt" rows="5" class="br-textarea"></textarea>
                      <div class="br-prompt-actions">
                        <button class="primary sm" :disabled="brState(item).loading" @click.stop="brGenerate(item)">{{ brState(item).loading ? '生成中...' : '生成' }}</button>
                        <button class="secondary sm" @click.stop="brCancelEdit(item)">取消</button>
                      </div>
                    </div>
                    <div v-else-if="brState(item).result" class="br-result">
                      <div v-if="brParsed(item)" class="br-parsed">
                        <p class="br-purpose">{{ brParsed(item).business_purpose_zh || brParsed(item).business_purpose_en }}</p>
                        <details v-if="brParsed(item).business_flow_zh?.length || brParsed(item).business_flow_en?.length" class="br-detail">
                          <summary>业务步骤</summary>
                          <ol><li v-for="(s, si) in (brParsed(item).business_flow_zh || brParsed(item).business_flow_en || [])" :key="si">{{ s }}</li></ol>
                        </details>
                        <details v-if="brParsed(item).business_rules?.length" class="br-detail">
                          <summary>业务规则 ({{ brParsed(item).business_rules.length }})</summary>
                          <ul><li v-for="(r, ri) in brParsed(item).business_rules" :key="ri">{{ r }}</li></ul>
                        </details>
                        <details v-if="brParsed(item).side_effects?.length" class="br-detail">
                          <summary>副作用</summary>
                          <ul><li v-for="(e, ei) in brParsed(item).side_effects" :key="ei">{{ e }}</li></ul>
                        </details>
                      </div>
                      <pre v-else class="br-raw">{{ brState(item).result }}</pre>
                      <div class="br-actions">
                        <button class="secondary sm" @click.stop="brStartEdit(item)">编辑提示词</button>
                        <button class="secondary sm" :disabled="brState(item).loading" @click.stop="brRetry(item)">{{ brState(item).loading ? '重试中...' : '重试' }}</button>
                      </div>
                    </div>
                    <button v-else class="secondary sm" :disabled="brState(item).loading" @click.stop="brStartEdit(item)">{{ brState(item).loading ? '生成中...' : '生成业务规则' }}</button>
                  </div>
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

    <div v-if="sequenceZoom" class="sequence-zoom-backdrop" @click.self="closeSequenceZoom">
      <section
        ref="sequenceZoomDialog"
        class="sequence-zoom-dialog"
        data-testid="sequence-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sequence-zoom-title"
        tabindex="-1"
        @keydown.esc="closeSequenceZoom"
      >
        <header class="sequence-zoom-header">
          <div>
            <h2 id="sequence-zoom-title">API 时序图</h2>
            <p><span class="method">{{ sequenceZoom.method }}</span> <code>{{ sequenceZoom.path }}</code></p>
          </div>
          <button class="sequence-zoom-close" type="button" aria-label="关闭放大时序图" @click="closeSequenceZoom">×</button>
        </header>
        <div class="sequence-zoom-canvas">
          <div class="sequence-zoom-controls" aria-label="时序图缩放控制">
            <button type="button" aria-label="缩小时序图" :disabled="sequenceZoomScale <= 0.5" @click="changeSequenceZoom(-0.25)">−</button>
            <span aria-live="polite">{{ Math.round(sequenceZoomScale * 100) }}%</span>
            <button type="button" aria-label="放大时序图" :disabled="sequenceZoomScale >= 3" @click="changeSequenceZoom(0.25)">+</button>
            <button type="button" @click="resetSequenceZoom">恢复原始大小</button>
          </div>
          <div class="sequence-zoom-stage" :style="{ width: `${sequenceZoomScale * 100}%` }">
            <pre ref="sequenceZoomMermaid" class="mermaid">{{ sequenceZoom.mermaid }}</pre>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'
import CallChainTree from '../components/CallChainTree.vue'

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
  components: { UiState, CallChainTree },
  props: { repoName: String },
  data() {
    return {
      report: null, activeSection: 'api_contract', llmLoaded: false, sections: SECTIONS,
      expandedKeys: new Set(), expandedEntityKeys: new Set(),
      endpointSearch: '', endpointMethod: '', endpointService: '', endpointPage: 1, endpointPageSize: 25,
      loadedAt: '', loadDuration: 0,
      businessRules: {}, brLoading: new Set(), brEditing: {},
      sequenceZoom: null,
      sequenceZoomScale: 1,
      apiExplanations: {},
      explanationPollTimers: {},
    }
  },
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
        if (this.endpointService && item.repository !== this.endpointService) return false
        if (!query) return true
        return [item.path, item.handler, item.repository].some(value => String(value || '').toLowerCase().includes(query))
      })
    },
    visibleEndpoints() { return this.filteredEndpoints.slice((this.endpointPage - 1) * this.endpointPageSize, this.endpointPage * this.endpointPageSize) },
    endpointPages() { return Math.max(1, Math.ceil(this.filteredEndpoints.length / this.endpointPageSize)) },
    endpointMethods() { return [...new Set((this.report?.api_contract?.endpoints || []).map(item => item.method).filter(Boolean))].sort() },
    endpointServices() { return [...new Set((this.report?.api_contract?.endpoints || []).map(item => item.repository).filter(Boolean))].sort() },
  },
  async created() {
    await this.load(false)
    await this.loadBusinessRules()
  },
  beforeUnmount() {
    for (const timer of Object.values(this.explanationPollTimers)) clearTimeout(timer)
  },
  methods: {
    async load(includeLlm) {
      const started = performance.now()
      await this.$runAsync(async () => {
        this.report = await this.$api.get('/api/knowledge', { repo: this.repoName || '', include_llm: includeLlm })
        for (const timer of Object.values(this.explanationPollTimers)) clearTimeout(timer)
        this.explanationPollTimers = {}
        this.apiExplanations = {}
        this.llmLoaded = includeLlm
        this.endpointPage = 1
        this.endpointService = ''
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
    explanationKey(item) { return [this.repoName || '', item.repository || '', item.method || '', item.path || '', item.handler || ''].join('||') },
    apiKey(item) { return item.api_key || [String(item.method || '').toUpperCase(), item.path || '', item.handler || ''].join('|') },
    explanationState(item) {
      return this.apiExplanations[this.explanationKey(item)] || { loading: false, running: false, current: null, snapshots: [], showSnapshots: false, deleting: '', error: '' }
    },
    setExplanationState(item, patch) {
      const key = this.explanationKey(item)
      this.apiExplanations = { ...this.apiExplanations, [key]: { ...this.explanationState(item), ...patch } }
    },
    toggleEntity(item) {
      const key = item.node_id || item.qualified_name
      if (this.expandedEntityKeys.has(key)) {
        this.expandedEntityKeys.delete(key)
      } else {
        this.expandedEntityKeys.add(key)
      }
      this.expandedEntityKeys = new Set(this.expandedEntityKeys)
    },
    toggleEndpoint(item, index) {
      const key = this.endpointKey(item, index)
      if (this.expandedKeys.has(key)) {
        this.expandedKeys.delete(key)
      } else {
        this.expandedKeys.add(key)
        if (!this.apiExplanations[this.explanationKey(item)]) this.loadEndpointExplanation(item)
      }
      // trigger reactivity for Set
      this.expandedKeys = new Set(this.expandedKeys)
    },
    explanationQuery(item) { return { repo: this.repoName || '', member: item.repository || '', api_key: this.apiKey(item) } },
    unwrapCurrent(data) {
      if (!data || data.status === 'missing') return null
      const snapshot = data.snapshot || data.current || data.current_snapshot || (data.id ? data : null)
      return snapshot && data.freshness ? { ...snapshot, freshness: data.freshness } : snapshot
    },
    unwrapSnapshots(data) { return Array.isArray(data) ? data : (data?.snapshots || []) },
    async loadEndpointExplanation(item, { quiet = false } = {}) {
      if (!quiet) this.setExplanationState(item, { loading: true, error: '' })
      try {
        const query = this.explanationQuery(item)
        const [currentData, snapshotsData] = await Promise.all([
          this.$api.get('/api/api-explanations/current', query).catch(err => {
            if (err.status === 404) return null
            throw err
          }),
          this.$api.get('/api/api-explanations/snapshots', query),
        ])
        const snapshots = this.unwrapSnapshots(snapshotsData)
        const running = snapshots.some(snapshot => ['pending', 'running'].includes(snapshot.status))
        const current = this.unwrapCurrent(currentData)
        this.setExplanationState(item, { current, snapshots, running, status: running ? 'running' : (current?.freshness === 'outdated' ? 'stale' : (current?.status || (current ? 'completed' : 'missing'))), loading: false, error: '' })
        if (running) this.scheduleExplanationPoll(item)
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || '读取解释失败'
        this.setExplanationState(item, { loading: false, error: detail })
        if (quiet && this.explanationState(item).running) this.scheduleExplanationPoll(item)
      }
    },
    async generateEndpointExplanation(item) {
      this.clearExplanationPoll(item)
      this.setExplanationState(item, { running: true, status: 'running', error: '' })
      try {
        const response = await this.$api.request('/api/api-explanations/generate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo: this.repoName || '', member: item.repository || '', method: item.method || '', path: item.path || '', handler: item.handler || '', file: item.file || '', line: Number(item.line || 0) }),
        })
        const snapshot = response.snapshot || response
        const snapshots = snapshot?.id ? [snapshot, ...this.explanationState(item).snapshots.filter(existing => existing.id !== snapshot.id)] : this.explanationState(item).snapshots
        this.setExplanationState(item, { snapshots, running: ['pending', 'running'].includes(snapshot?.status || 'running') })
        if (['completed', 'partial', 'failed'].includes(snapshot?.status)) await this.loadEndpointExplanation(item, { quiet: true })
        else this.scheduleExplanationPoll(item)
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || '生成解释失败'
        this.setExplanationState(item, { running: false, status: 'failed', error: detail })
      }
    },
    scheduleExplanationPoll(item) {
      const key = this.explanationKey(item)
      this.clearExplanationPoll(item)
      this.explanationPollTimers[key] = setTimeout(async () => { await this.loadEndpointExplanation(item, { quiet: true }) }, 1200)
    },
    clearExplanationPoll(item) {
      const key = this.explanationKey(item)
      if (this.explanationPollTimers[key]) clearTimeout(this.explanationPollTimers[key])
      delete this.explanationPollTimers[key]
    },
    async toggleExplanationSnapshots(item) {
      const showSnapshots = !this.explanationState(item).showSnapshots
      this.setExplanationState(item, { showSnapshots })
      if (showSnapshots) await this.loadEndpointExplanation(item, { quiet: true })
    },
    async deleteExplanationSnapshot(item, snapshot) {
      const isCurrent = this.explanationState(item).current?.id === snapshot.id
      const warning = isCurrent ? '这是当前解释快照，删除后该 API 将进入无快照状态。是否删除？' : '是否删除该解释快照？'
      if (!window.confirm(warning)) return
      this.setExplanationState(item, { deleting: snapshot.id, error: '' })
      try {
        const suffix = isCurrent ? '?confirm_current=true' : ''
        await this.$api.delete(`/api/api-explanations/snapshots/${encodeURIComponent(snapshot.id)}${suffix}`)
        await this.loadEndpointExplanation(item, { quiet: true })
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || '删除快照失败'
        this.setExplanationState(item, { error: detail })
      } finally { this.setExplanationState(item, { deleting: '' }) }
    },
    snapshotExplanation(snapshot) {
      const value = snapshot?.explanation || snapshot?.aggregate_explanation
      if (!value) return null
      if (typeof value === 'object') return value
      try { return JSON.parse(value) } catch { return { summary: value } }
    },
    explanationSteps(snapshot) {
      const explanation = this.snapshotExplanation(snapshot) || {}
      return explanation.main_flow || explanation.business_flow || explanation.business_flow_zh || explanation.steps || []
    },
    explanationStepText(step) { return typeof step === 'string' ? step : (step.detail || step.summary || step.title || JSON.stringify(step)) },
    snapshotCoverage(snapshot) {
      const value = snapshot?.coverage || snapshot?.statistics
      if (!value) return null
      if (typeof value === 'object') return value
      try { return JSON.parse(value) } catch { return null }
    },
    coverageValue(snapshot, ...keys) {
      const coverage = this.snapshotCoverage(snapshot) || {}
      for (const key of keys) if (coverage[key] !== undefined && coverage[key] !== null) return coverage[key]
      return 0
    },
    coveragePercent(snapshot) {
      const coverage = this.snapshotCoverage(snapshot) || {}
      if (coverage.coverage_pct !== undefined) return `${coverage.coverage_pct}%`
      const total = this.coverageValue(snapshot, 'total_nodes', 'nodes_total'); const completed = this.coverageValue(snapshot, 'completed_nodes', 'translated_nodes')
      return total ? `${Math.round(completed / total * 100)}%` : '-'
    },
    snapshotNodes(snapshot) { return snapshot?.nodes || snapshot?.node_explanations || [] },
    nodeSummary(node) {
      const value = node.aggregate_explanation || node.local_explanation || node.explanation
      if (!value) return ''
      if (typeof value === 'object') return value.summary || value.business_purpose_zh || value.business_purpose_en || ''
      try { const parsed = JSON.parse(value); return parsed.summary || parsed.business_purpose_zh || parsed.business_purpose_en || '' } catch { return value }
    },
    explanationStatusText(status) { return ({ pending: '等待中', running: '生成中', completed: '已完成', partial: '部分完成', failed: '失败', stale: '已过期', missing: '未生成' })[status] || status || '' },
    shortRevision(revision) { return String(revision || '').slice(0, 10) },
    formatSnapshotTime(value) {
      if (!value) return ''
      const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value)
      return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
    },
    // Render the sequence diagram only once its <details> is actually open.
    seqToggle(event) {
      if (event.target && event.target.open) {
        this.$nextTick(() => this.renderMermaid())
      }
    },
    async openSequenceZoom(item) {
      this.sequenceZoomScale = 1
      this.sequenceZoom = {
        method: item.method || '',
        path: item.path || '',
        mermaid: item.call_chain_mermaid,
      }
      await this.$nextTick()
      this.$refs.sequenceZoomDialog?.focus()
      await this.renderMermaidNodes([this.$refs.sequenceZoomMermaid])
    },
    closeSequenceZoom() {
      this.sequenceZoom = null
    },
    changeSequenceZoom(delta) {
      this.sequenceZoomScale = Math.min(3, Math.max(0.5, this.sequenceZoomScale + delta))
    },
    resetSequenceZoom() {
      this.sequenceZoomScale = 1
    },
    // ── business rules ──

    brKey(item) { return [this.repoName, item.handler, item.method, item.path].join('||') },

    async loadBusinessRules() {
      try {
        const data = await this.$api.get('/api/business-rules', { repo: this.repoName || '' })
        const map = {}
        for (const r of data.rules || []) {
          map[[r.repo_name, r.handler, r.method, r.path].join('||')] = r
        }
        this.businessRules = map
      } catch { /* ignore */ }
    },

    brState(item) {
      const key = this.brKey(item)
      const rule = this.businessRules[key]
      const loading = this.brLoading.has(key)
      const edit = this.brEditing[key]
      let status = ''; let statusText = ''
      if (loading) { status = 'loading'; statusText = '生成中...' }
      else if (rule?.status === 'completed') { status = 'completed'; statusText = '已生成' }
      else if (rule?.status === 'failed') { status = 'failed'; statusText = '失败' }
      return {
        rule, loading, status, statusText,
        result: rule?.result || '',
        editing: !!edit,
        editPrompt: edit || rule?.custom_prompt || this._defaultPrompt(item),
      }
    },

    brParsed(item) {
      const result = this.brState(item).result
      if (!result) return null
      try {
        const parsed = JSON.parse(result)
        if (parsed && typeof parsed === 'object' && !parsed.error && (parsed.business_purpose_en || parsed.business_purpose_zh)) {
          return parsed
        }
      } catch { return null }
      return null
    },

    _defaultPrompt(item) {
      return `You are a senior software architect explaining API business logic to product managers and developers.

Below is the call chain sequence diagram (Mermaid sequenceDiagram) for an API endpoint. Analyze it and explain:

1. **Business purpose**: What business function does this API serve? (1-2 sentences in English + Chinese)
2. **Business flow**: Walk through the call chain step by step in business terms.
3. **Key business rules**: What business constraints or decisions are embedded?
4. **Side effects**: What external systems or services are affected?

Sequence diagram:
\`\`\`
${item.call_chain_mermaid || `sequenceDiagram\n    participant N0 as ${item.handler}\n    Note over N0: No downstream calls`}
\`\`\`

API endpoint: ${item.method} ${item.path}
Handler: ${item.handler}

Output JSON:
{
  "business_purpose_en": "...",
  "business_purpose_zh": "...",
  "business_flow_en": ["Step 1: ...", ...],
  "business_flow_zh": ["步骤1: ...", ...],
  "business_rules": ["Rule 1: ...", ...],
  "side_effects": ["Effect 1: ...", ...]
}

JSON:`
    },

    brStartEdit(item) {
      const key = this.brKey(item)
      this.brEditing[key] = this.brState(item).editPrompt
    },

    brCancelEdit(item) {
      const key = this.brKey(item)
      delete this.brEditing[key]
    },

    async brGenerate(item) {
      const key = this.brKey(item)
      const prompt = this.brEditing[key] || this.brState(item).editPrompt
      this.brLoading.add(key)
      try {
        const resp = await this.$api.request('/api/business-rules/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            repo: this.repoName || '',
            handler: item.handler || '',
            method: item.method || '',
            path: item.path || '',
            call_chain_mermaid: item.call_chain_mermaid || '',
            custom_prompt: prompt || '',
          }),
        })
        this.businessRules[key] = {
          id: resp.id, result: resp.result, status: 'completed',
          repo_name: this.repoName, handler: item.handler, method: item.method, path: item.path,
          custom_prompt: prompt,
        }
        delete this.brEditing[key]
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || 'Unknown error'
        this.businessRules[key] = {
          ...this.businessRules[key],
          status: 'failed', error: detail,
          repo_name: this.repoName, handler: item.handler, method: item.method, path: item.path,
          custom_prompt: prompt,
        }
        alert('生成失败: ' + detail)
        delete this.brEditing[key]
      } finally {
        this.brLoading = new Set([...this.brLoading].filter(k => k !== key))
      }
    },

    async brRetry(item) {
      await this.brGenerate(item)
    },

    async renderMermaid() {
      const els = this.$el.querySelectorAll('.expand-detail .mermaid:not([data-processed])')
      await this.renderMermaidNodes(els)
    },
    async renderMermaidNodes(nodes) {
      const els = [...nodes].filter(Boolean)
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
.seq-details { margin: 0 0 15px; }
.seq-details summary { cursor: pointer; color: #2a6496; font-size: 12px; padding: 2px 0; user-select: none; }
.seq-details summary:hover { text-decoration: underline; }
.seq-actions { display: flex; justify-content: flex-end; margin: 6px 0; }
.sequence-zoom-backdrop { position: fixed; z-index: 100; inset: 0; padding: 24px; display: grid; place-items: center; background: rgba(17, 17, 28, .72); }
.sequence-zoom-dialog { width: min(1400px, 96vw); height: min(900px, 92vh); display: flex; flex-direction: column; overflow: hidden; background: white; border-radius: 10px; box-shadow: 0 24px 80px rgba(0, 0, 0, .42); }
.sequence-zoom-header { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 14px 18px; border-bottom: 1px solid #e5e5eb; }
.sequence-zoom-header h2 { margin: 0 0 4px; font-size: 18px; }
.sequence-zoom-header p { margin: 0; font-size: 12px; color: #666; }
.sequence-zoom-close { width: 36px; height: 36px; border: 0; border-radius: 50%; background: #f0f0f4; color: #444; font-size: 24px; line-height: 1; cursor: pointer; }
.sequence-zoom-close:hover { background: #e94560; color: white; }
.sequence-zoom-canvas { position: relative; flex: 1; min-height: 0; overflow: auto; padding: 64px 24px 24px; background: #fafafd; }
.sequence-zoom-controls { position: absolute; z-index: 1; top: 14px; right: 18px; display: flex; align-items: center; gap: 6px; padding: 5px; border: 1px solid #dddde5; border-radius: 7px; background: rgba(255, 255, 255, .94); box-shadow: 0 2px 8px rgba(0, 0, 0, .08); }
.sequence-zoom-controls button { border: 1px solid #d7d7df; border-radius: 5px; padding: 5px 9px; background: white; color: #333; cursor: pointer; }
.sequence-zoom-controls button:disabled { cursor: not-allowed; }
.sequence-zoom-controls span { min-width: 44px; text-align: center; color: #555; font-size: 12px; }
.sequence-zoom-stage { transition: width .15s ease; }
.sequence-zoom-stage .mermaid { display: flex; justify-content: center; }
.sequence-zoom-stage :deep(svg) { width: 100% !important; max-width: none !important; height: auto; }
.frontend-call { border-left: 3px solid #e94560; padding: 5px 9px; margin: 6px 0; font-size: 11px; }
.muted { color: #aaa; font-size: 11px; }
.repo-badge { display: inline-block; margin-right: 5px; padding: 1px 5px; border-radius: 8px; background: #fff0f2; color: #c82d48; font-size: 9px; }
.api-explanation-section { margin-top: 16px; border-top: 1px solid #e2e2e8; padding-top: 12px; }
.api-explanation-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.api-explanation-heading h4 { margin-bottom: 3px; font-size: 13px; color: #333; }
.api-explanation-actions { display: flex; gap: 6px; flex-shrink: 0; }
.explanation-status { display: inline-block; margin-left: 5px; padding: 2px 6px; border-radius: 8px; background: #eeeef3; color: #666; font-size: 10px; font-weight: 400; }
.explanation-running, .explanation-pending { background: #e3f0fc; color: #2a6496; }
.explanation-completed { background: #eaf8f0; color: #23764a; }
.explanation-partial, .explanation-stale { background: #fff3d6; color: #8a6500; }
.explanation-failed { background: #ffeaea; color: #b8324a; }
.explanation-progress { margin: 9px 0; padding: 8px 10px; border-radius: 5px; background: #eef7ff; color: #2a6496; font-size: 11px; }
.explanation-error { margin: 9px 0; color: #b8324a; font-size: 11px; }
.explanation-current { margin-top: 9px; padding: 11px; border: 1px solid #e5e5eb; border-radius: 6px; background: white; }
.explanation-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: #888; font-size: 10px; }
.explanation-summary { margin: 9px 0; color: #333; font-size: 12px; line-height: 1.55; }
.explanation-body details, .node-explanations { margin-top: 7px; font-size: 11px; }
.explanation-body summary, .node-explanations > summary { cursor: pointer; color: #555; }
.explanation-body ol { margin: 5px 0 0 18px; }
.coverage-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.coverage-grid span { padding: 3px 7px; border-radius: 4px; background: #f2f2f6; color: #555; font-size: 10px; }
.node-explanation { margin-top: 6px; padding: 7px 8px; border-left: 2px solid #ddd; background: #fafafd; }
.node-explanation header { display: flex; justify-content: space-between; gap: 8px; }
.node-explanation p { margin: 5px 0; color: #444; line-height: 1.45; }
.node-explanation small { color: #999; }
.snapshot-list { margin-top: 10px; padding: 10px; border: 1px solid #e5e5eb; border-radius: 6px; background: #fafafd; }
.snapshot-list h5 { margin-bottom: 6px; font-size: 11px; color: #555; }
.snapshot-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 0; border-top: 1px solid #e8e8ed; font-size: 11px; }
.snapshot-item:first-of-type { border-top: 0; }
.snapshot-item small { display: block; margin-top: 3px; color: #999; }
.business-rule-section { margin-top: 16px; border-top: 1px solid #eee; padding-top: 12px; }
.business-rule-section h4 { font-size: 13px; color: #444; margin-bottom: 8px; }
.br-status { font-size: 10px; padding: 2px 6px; border-radius: 8px; margin-left: 6px; font-weight: 400; }
.br-loading { background: #e3f0fc; color: #2a6496; }
.br-completed { background: #eaf8f0; color: #23764a; }
.br-failed { background: #ffeaea; color: #b8324a; }
.br-prompt-edit { display: grid; gap: 8px; }
.br-textarea { width: 100%; border: 1px solid #cfd3da; border-radius: 5px; padding: 8px; font: 11px/1.5 monospace; resize: vertical; }
.br-prompt-actions { display: flex; gap: 6px; }
.br-result { font-size: 12px; }
.br-parsed { display: grid; gap: 6px; }
.br-purpose { color: #333; line-height: 1.5; }
.br-detail { font-size: 11px; }
.br-detail summary { color: #555; cursor: pointer; padding: 3px 0; }
.br-detail ol, .br-detail ul { margin: 4px 0 4px 18px; }
.br-detail li { padding: 2px 0; color: #444; line-height: 1.4; }
.br-raw { max-height: 200px; overflow: auto; font-size: 10px; padding: 8px; background: #f5f5f8; border-radius: 4px; white-space: pre-wrap; }
.br-actions { display: flex; gap: 6px; margin-top: 8px; }
.sm { padding: 5px 10px; font-size: 11px; }
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
  .sequence-zoom-backdrop { padding: 0; }
  .sequence-zoom-dialog { width: 100vw; height: 100vh; border-radius: 0; }
  .api-explanation-heading { display: block; }
  .api-explanation-actions { margin-top: 8px; }
}
</style>
