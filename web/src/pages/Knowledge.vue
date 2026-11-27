<template>
  <div class="knowledge">
    <template v-if="!snapshotId">
      <div class="page-header">
        <div><h1>{{ t('知识中心') }}</h1><p>{{ t('选择一个已发布的项目 Snapshot，查看该项目的结构知识。') }}</p></div>
        <router-link class="secondary" :to="{ name: 'snapshots' }">{{ t('管理 Snapshots') }}</router-link>
      </div>
      <UiState v-if="catalogError" kind="error" :title="t('项目知识目录加载失败')" :message="catalogError.message" :action-label="t('重试')" @action="loadCatalog" />
      <UiState v-else-if="catalogLoading" kind="loading" :title="t('正在加载项目知识目录')" />
      <section v-else-if="projectCatalog.length" class="project-list" data-testid="knowledge-projects">
        <article v-for="project in projectCatalog" :key="project.member_id" class="project-card" data-testid="knowledge-project">
          <div>
            <h2>{{ project.display_name }}</h2>
            <p>{{ project.scope_name }} · {{ t('已发布 Snapshot') }}</p>
            <code>{{ project.snapshot_id }}</code>
          </div>
          <router-link class="primary" :to="knowledgeLink(project)">{{ t('查看知识') }}</router-link>
        </article>
      </section>
      <UiState v-else kind="empty" :title="t('还没有已发布的项目知识')" :message="t('请先在 Snapshots 中运行分析并发布 Snapshot。')">
        <router-link class="primary" :to="{ name: 'snapshots' }">{{ t('前往 Snapshots') }}</router-link>
      </UiState>
    </template>

    <template v-else>
    <UiState v-if="error" kind="error" :title="t('知识提取失败')" :message="error.message" :action-label="t('重试')" :dismiss-label="t('关闭')" @action="load(false)" @dismiss="error = null" />
    <UiState v-if="loading" kind="loading" :title="t('正在从 CodeGraph 提取结构知识')" :message="t('大型仓库可能需要等待片刻，完成前可以继续浏览当前结果。')" />

    <div v-if="snapshotId" class="page-header">
      <div>
        <h1>{{ repoName || t('知识中心') }}</h1>
        <p>{{ t('基于不可变 Repository Snapshot 推导。') }} <code v-if="snapshotId">{{ snapshotId }}</code><span v-if="loadedAt"> · {{ loadedAt }} · {{ loadDuration }} ms</span></p>
      </div>
      <div class="actions">
        <button class="secondary" :disabled="loading" @click="load(false)">{{ t('刷新结构知识') }}</button>
        <button class="primary" :disabled="loading || llmJobActive" @click="loadLlm">
          {{ llmJobActive ? t('正在抽取 LLM 知识') : (llmLoaded ? t('重新抽取 LLM 知识') : t('抽取 LLM 知识')) }}
        </button>
      </div>
    </div>

    <div class="llm-progress" v-if="snapshotId && llmJobActive" data-testid="llm-progress">
      <div class="llm-progress-heading"><span>{{ t('LLM 知识抽取进行中') }}</span><strong>{{ llmProgress.percent }}%</strong></div>
      <div class="llm-progress-track"><span :style="{ width: `${llmProgress.percent}%` }"></span></div>
      <p>{{ t('当前阶段：{stage}', { stage: llmStageText(llmJob.progress?.stage) }) }} · {{ t('已完成 {completed}/{total} 个阶段', { completed: llmProgress.completed, total: llmProgress.total }) }}</p>
    </div>
    <div class="notice" v-if="snapshotId && !llmLoaded && !llmJobActive">
      {{ t('业务描述、业务规则、错误目录和状态机需要 LLM，可通过页面顶部“LLM 设置”配置，仅在点击抽取时调用。') }}
    </div>
    <div class="llm-error" v-if="snapshotId && llmJob?.status === 'failed'">
      {{ t('LLM 知识抽取失败') }}：{{ llmJob.error_message || t('未知错误') }}
    </div>

    <div class="summary-grid" v-if="snapshotId && report">
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

    <div class="content" v-if="snapshotId && report">
      <aside ref="sectionNav" class="section-nav">
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

        <template v-if="activeSection === 'terms'">
          <div class="terms-toolbar"><span>{{ t('术语已在分析发布后自动生成，并按可信度降序排列。') }}</span><router-link class="secondary sm" :to="{ name: 'terms', query: { snapshot_id: snapshotId } }">{{ t('打开术语审核') }}</router-link></div>
          <div v-if="termsLoading" class="empty-semantic"><p>{{ t('正在加载术语') }}</p></div>
          <div v-else-if="terms.length" class="table-wrap"><table><thead><tr><th>#</th><th>{{ t('规范名') }}</th><th>{{ t('类型') }}</th><th>{{ t('可信度') }}</th><th>{{ t('状态') }}</th><th>{{ t('证据') }}</th></tr></thead><tbody><tr v-for="term in terms" :key="term.id"><td>{{ term.rank || '-' }}</td><td><b>{{ term.canonical_name }}</b><small v-if="term.aliases?.length">{{ term.aliases.join(', ') }}</small></td><td><code>{{ termTypeLabel(term.term_type) }}</code></td><td><span class="term-score">{{ Math.round(Number(term.confidence_score || 0) * 100) }}%</span><small>{{ termConfidenceBandLabel(term.confidence_band) }}</small></td><td><span class="term-status" :class="term.status">{{ termStatusLabel(term.status) }}</span></td><td>{{ term.evidence_ids?.length || term.evidence_count || 0 }}</td></tr></tbody></table></div>
          <div v-else class="empty-semantic"><p>{{ t('当前 Snapshot 尚未生成术语。') }}</p></div>
        </template>

        <template v-if="activeSection === 'api_contract'">
          <div class="metric">{{ activeData.endpoint_count || 0 }} <small>{{ t('个端点') }}</small></div>
          <section class="api-prompt-panel" data-testid="api-explanation-settings">
            <div class="api-prompt-heading"><div><h3>{{ t('API 解释设置') }}</h3><p>{{ t('提示词按当前 Snapshot 保存版本，并可应用到全部端点。模板结果同时供 Agent 设计方案使用。') }}</p></div><span v-if="promptCurrent">{{ t('当前版本') }} v{{ promptCurrent.version }}</span></div>
            <div class="prompt-default-heading"><span>{{ t('系统默认模板') }}</span><button class="secondary sm" type="button" :disabled="promptSaving || !promptDefaults.local" @click="restoreAllPromptTemplates">{{ t('恢复全部默认') }}</button></div>
            <p class="prompt-default-hint">{{ t('编辑框显示当前生效模板；可展开查看系统默认值，并在此基础上微调。') }}</p>
            <details v-if="promptDefaults.local" class="prompt-default-reference">
              <summary>{{ t('查看系统默认模板') }}</summary>
              <div class="prompt-default-item"><b>{{ t('自身源码 / 代码块模板') }}</b><pre>{{ promptDefaults.local }}</pre></div>
              <div class="prompt-default-item"><b>{{ t('多代码块合并模板') }}</b><pre>{{ promptDefaults.synthesis }}</pre></div>
              <div class="prompt-default-item"><b>{{ t('节点聚合模板') }}</b><pre>{{ promptDefaults.aggregate }}</pre></div>
            </details>
            <label class="prompt-field"><span>{{ t('统一业务指导') }}</span><textarea v-model.trim="promptText" rows="3" :placeholder="t('例如：重点关注订单状态变化、库存扣减和异常处理')"></textarea></label>
            <label class="prompt-field"><span class="prompt-field-heading"><span>{{ t('自身源码 / 代码块模板') }}</span><button class="link-button" type="button" :disabled="promptSaving || !promptDefaults.local" @click="restorePromptTemplate('local')">{{ t('恢复默认') }}</button></span><textarea v-model="promptTemplates.local" rows="10"></textarea></label>
            <label class="prompt-field"><span class="prompt-field-heading"><span>{{ t('多代码块合并模板') }}</span><button class="link-button" type="button" :disabled="promptSaving || !promptDefaults.synthesis" @click="restorePromptTemplate('synthesis')">{{ t('恢复默认') }}</button></span><textarea v-model="promptTemplates.synthesis" rows="8"></textarea></label>
            <label class="prompt-field"><span class="prompt-field-heading"><span>{{ t('节点聚合模板') }}</span><button class="link-button" type="button" :disabled="promptSaving || !promptDefaults.aggregate" @click="restorePromptTemplate('aggregate')">{{ t('恢复默认') }}</button></span><textarea v-model="promptTemplates.aggregate" rows="10"></textarea></label>
            <p class="prompt-hint">{{ t('可用变量：{qualified_name}、{source}、{local_explanation}、{children_explanations}、{guidance} 等；JSON 输出约束不可删除。') }}</p>
            <div class="api-prompt-actions"><button class="secondary sm" :disabled="promptSaving || !promptReady" @click="savePrompt">{{ promptSaving ? t('保存中...') : t('保存提示词') }}</button><button class="primary sm" :disabled="promptSaving || batchActive || !promptReady" @click="saveAndApplyBatch">{{ batchActive ? t('批量任务进行中') : t('保存并应用到全部端点') }}</button><button class="secondary sm" data-testid="explanation-refresh-stale" :disabled="promptSaving || batchActive || !explanationStatusSummary.stale" @click="refreshStaleBatch">{{ t('刷新过期端点（{count}）', { count: explanationStatusSummary.stale }) }}</button><button class="secondary sm" data-testid="explanation-export" :disabled="exporting || !snapshotId" @click="exportApiExplanations">{{ exporting ? t('导出中...') : t('导出 API 契约') }}</button></div>
            <div v-if="promptError" class="explanation-error">{{ promptError }}</div>
            <details v-if="promptProfiles.length" class="prompt-history"><summary>{{ t('历史提示词版本（{count}）', { count: promptProfiles.length }) }}</summary><div v-for="profile in promptProfiles" :key="profile.id" class="prompt-history-item"><code>v{{ profile.version }}</code><span>{{ profile.prompt_text }}</span><small>{{ profile.is_current ? t('当前') : '' }}</small></div></details>
          </section>
          <section v-if="batchJob" class="batch-card" data-testid="api-explanation-batch">
            <div class="batch-heading"><div><h3>{{ t('API 解释批量任务') }} <span class="explanation-status" :class="'explanation-' + batchJob.status">{{ batchStatusText(batchJob.status) }}</span></h3><p>{{ t('任务') }} {{ batchJob.id }} · {{ t('提示词版本') }} {{ batchJob.prompt_profile_id ? promptVersion(batchJob.prompt_profile_id) : t('系统默认') }}</p></div><strong>{{ batchJob.progress?.percent || 0 }}%</strong></div>
            <div class="llm-progress-track"><span :style="{ width: `${batchJob.progress?.percent || 0}%` }"></span></div>
            <p class="batch-counts">{{ t('已完成 {completed}/{total}', { completed: batchJob.progress?.completed || 0, total: batchJob.progress?.total || 0 }) }} · {{ t('失败 {count}', { count: batchJob.progress?.failed || 0 }) }} · {{ t('跳过 {count}', { count: batchJob.progress?.skipped || 0 }) }} · {{ t('执行中 {count}', { count: batchJob.progress?.running || 0 }) }} · {{ t('取消 {count}', { count: batchJob.progress?.cancelled || 0 }) }}</p>
            <div class="api-prompt-actions"><button v-if="batchActive && !batchJob.cancel_requested" class="secondary sm" @click="cancelBatch">{{ t('取消任务') }}</button><span v-if="batchJob.cancel_requested && batchActive" class="batch-cancelling">{{ t('取消中') }}</span><button v-if="batchJob.progress?.failed && !batchActive" class="secondary sm" @click="retryBatch">{{ t('重试失败') }}</button></div>
            <p v-if="batchJob.cancel_requested && batchActive" class="batch-cancel-hint">{{ t('取消请求已提交，正在等待当前端点安全停止…') }}</p>
            <details class="batch-items"><summary>{{ t('查看端点明细（{count}）', { count: batchJob.items?.length || 0 }) }}</summary><div v-for="item in batchJob.items || []" :key="item.id" class="batch-item"><code>{{ item.method }} {{ item.path }}</code><span class="explanation-status" :class="'explanation-' + item.status">{{ batchStatusText(item.status) }}</span><small v-if="item.explanation_snapshot_id">{{ item.explanation_snapshot_id }}</small><small v-else-if="item.error_message || item.skip_reason">{{ item.error_message || item.skip_reason }}</small></div></details>
          </section>
          <div class="table-tools">
            <label>{{ t('筛选端点') }}<input v-model.trim="endpointSearch" type="search" :placeholder="t('路径、处理函数或仓库')" @input="endpointPage = 1" /></label>
            <label>{{ t('HTTP 方法') }}<select v-model="endpointMethod" @change="endpointPage = 1"><option value="">{{ t('全部方法') }}</option><option v-for="method in endpointMethods" :key="method">{{ method }}</option></select></label>
            <label>{{ t('服务') }}<select v-model="endpointService" data-testid="endpoint-service-filter" @change="endpointPage = 1"><option value="">{{ t('全部服务') }}</option><option v-for="service in endpointServices" :key="service">{{ service }}</option></select></label>
            <span>{{ t('共 {count} 条', { count: filteredEndpoints.length }) }}</span>
            <span class="explanation-status-summary" data-testid="explanation-status-summary" :class="{ loading: explanationStatusLoading }">{{ t('已解释 {explained} · 待刷新 {stale} · 待解释 {pending}', explanationStatusSummary) }}</span>
          </div>
          <div class="table-wrap"><table class="api-contract-table"><thead><tr><th>{{ t('方法') }}</th><th>{{ t('路径') }}</th><th>{{ t('处理函数') }}</th><th>{{ t('请求/应答') }}</th><th>{{ t('前端调用') }}</th><th>{{ t('解释状态') }}</th></tr></thead><tbody>
            <template v-for="(item, index) in visibleEndpoints" :key="`${item.repository || ''}-${item.method}-${item.path}-${index}`">
              <tr class="clickable" :class="{ expanded: expandedKeys.has(endpointKey(item, index)) }" tabindex="0" @click="toggleEndpoint(item, index)" @keydown.enter="toggleEndpoint(item, index)">
                <td><span class="method">{{ item.method }}</span></td><td><code>{{ item.path }}</code></td><td>{{ item.handler || '-' }}</td>
                <td>{{ item.request_body?.type || t('无请求体') }} → {{ item.response_body?.type || item.return_type || t('未知') }}</td>
                <td>{{ item.frontend_callers?.length || 0 }} {{ t('处') }}</td>
                <td><span class="explanation-status" :class="'explanation-' + endpointStatusFor(item)">{{ endpointStatusText(endpointStatusFor(item)) }}</span></td>
              </tr>
              <tr v-if="expandedKeys.has(endpointKey(item, index))" class="expand-detail">
                <td colspan="6">
                  <div class="contract-grid">
                    <div><h4>{{ t('请求头') }}</h4><pre>{{ formatJson(item.request_headers || []) }}</pre></div>
                    <div><h4>{{ t('路径/查询参数') }}</h4><pre>{{ formatJson({ path: item.path_params || [], query: item.query_params || [] }) }}</pre></div>
                    <div><h4>{{ t('请求体') }}</h4><pre>{{ formatJson(item.request_body) }}</pre></div>
                    <div><h4>{{ t('应答体') }}</h4><pre>{{ formatJson(item.response_body) }}</pre></div>
                  </div>
                  <h4>{{ t('后端调用链') }}</h4>
                  <CallChainTree
                    v-if="item.handler && item.file && item.line"
                    :repo="repoName"
                    :member="item.repository"
                    :file="item.file"
                    :line="item.line"
                    :label="{ method: item.method, path: item.path, handler: item.handler, node_id: item.node_id || item.handler }"
                    :snapshot-id="snapshotId"
                    :explanation-snapshot="explanationState(item).current"
                    :explanation-state="explanationState(item)"
                    @generate-api="generateEndpointExplanation(item)"
                    @manage-api-explanations="toggleExplanationSnapshots(item)"
                  />
                  <p class="muted" v-else>{{ t('未解析到处理函数（file/line 缺失），无法展示调用链树。') }}</p>
                  <details v-if="item.call_chain_mermaid" class="seq-details" @toggle="seqToggle($event, item)">
                    <summary>{{ t('展开时序图') }}</summary>
                    <div class="seq-actions">
                      <button class="secondary sm" type="button" data-testid="sequence-expand" @click.stop="openSequenceZoom(item)">{{ t('放大查看') }}</button>
                    </div>
                    <div class="mermaid-wrap"><pre class="mermaid">{{ item.call_chain_mermaid }}</pre></div>
                  </details>
                  <div v-if="!item.call_chain_mermaid && item.call_chain?.length" class="call-chain"><span v-for="(node, idx) in item.call_chain || []" :key="node.id || node.name">{{ node.name }}<b v-if="idx < item.call_chain.length - 1">→</b></span></div>
                  <h4>{{ t('前端调用位置') }}</h4>
                  <div class="frontend-call" v-for="call in item.frontend_callers || []" :key="call.definition_file + call.function">
                    <b>{{ call.function }}</b> · <code>{{ call.definition_file }}:{{ call.definition_line }}</code>
                    <div v-for="site in call.call_sites" :key="site.file + site.line"><code>{{ site.file }}:{{ site.line }}</code></div>
                  </div>
                  <p class="muted" v-if="!item.frontend_callers?.length">{{ t('未匹配到前端调用。') }}</p>
                  <section class="api-explanation-section" data-testid="api-explanation">
                    <div class="api-explanation-heading">
                      <div>
                        <h4>{{ t('API 功能解释') }}
                          <span v-if="explanationState(item).status" class="explanation-status" :class="'explanation-' + explanationState(item).status">{{ explanationStatusText(explanationState(item).status) }}</span>
                        </h4>
                        <p class="muted">{{ t('解释由调用链叶子节点向入口聚合，仅在手动触发时调用模型。') }}</p>
                      </div>
                      <div class="api-explanation-actions">
                        <button class="primary sm" type="button" data-testid="explanation-generate" :disabled="explanationState(item).running || !item.handler || !item.file || !item.line" @click.stop="generateEndpointExplanation(item)">{{ explanationState(item).current ? t('手动刷新解释') : t('生成 API 功能解释') }}</button>
                        <button class="secondary sm" type="button" @click.stop="toggleExplanationSnapshots(item)">{{ explanationState(item).showSnapshots ? t('收起快照') : t('管理快照') }}</button>
                        <button class="secondary sm" type="button" data-testid="endpoint-prompt-button" :disabled="explanationState(item).running" @click.stop="openEndpointPrompt(item)">{{ t('自定义提示词') }}</button>
                      </div>
                    </div>
                    <p v-if="explanationState(item).loading" class="muted">{{ t('正在读取解释快照…') }}</p>
                    <p v-else-if="explanationState(item).error" class="explanation-error">{{ explanationState(item).error }}</p>
                    <p v-if="explanationState(item).running" class="explanation-progress">{{ t('正在生成候选快照，当前解释仍可正常查看…') }}</p>
                    <div v-if="explanationState(item).current" class="explanation-current">
                      <div class="explanation-meta">
                        <span>{{ t('快照') }} {{ explanationState(item).current.id }}</span>
                        <span v-if="explanationState(item).current.source_revision">{{ t('源码') }} {{ shortRevision(explanationState(item).current.source_revision) }}</span>
                        <span v-if="explanationState(item).current.model_id || explanationState(item).current.model">{{ t('模型') }} {{ explanationState(item).current.model_id || explanationState(item).current.model }}</span>
                      </div>
                      <div v-if="snapshotExplanation(explanationState(item).current)" class="explanation-body">
                        <p class="explanation-summary">{{ snapshotExplanation(explanationState(item).current).summary || snapshotExplanation(explanationState(item).current).business_purpose_zh || snapshotExplanation(explanationState(item).current).business_purpose_en }}</p>
                        <details v-if="explanationSteps(explanationState(item).current).length"><summary>{{ t('业务流程（{count}）', { count: explanationSteps(explanationState(item).current).length }) }}</summary><ol><li v-for="(step, stepIndex) in explanationSteps(explanationState(item).current)" :key="stepIndex">{{ explanationStepText(step) }}</li></ol></details>
                      </div>
                      <div class="coverage-grid" v-if="snapshotCoverage(explanationState(item).current)">
                        <span>{{ t('节点') }} {{ coverageValue(explanationState(item).current, 'completed_nodes', 'translated_nodes') }}/{{ coverageValue(explanationState(item).current, 'total_nodes', 'nodes_total') }}</span>
                        <span>{{ t('完整') }} {{ coveragePercent(explanationState(item).current) }}</span>
                        <span v-if="coverageValue(explanationState(item).current, 'partial_nodes')">{{ t('部分') }} {{ coverageValue(explanationState(item).current, 'partial_nodes') }}</span>
                        <span v-if="coverageValue(explanationState(item).current, 'failed_nodes')">{{ t('失败') }} {{ coverageValue(explanationState(item).current, 'failed_nodes') }}</span>
                      </div>
                      <p v-if="partialWarning(explanationState(item).current)" class="explanation-warning" data-testid="explanation-partial-warning">{{ partialWarning(explanationState(item).current) }}</p>
                      <details v-if="snapshotNodes(explanationState(item).current).length" class="node-explanations">
                        <summary>{{ t('节点解释状态（{count}）', { count: snapshotNodes(explanationState(item).current).length }) }}</summary>
                        <article v-for="node in snapshotNodes(explanationState(item).current)" :key="node.node_key" class="node-explanation">
                          <header><code>{{ node.node_key }}</code><span class="explanation-status" :class="'explanation-' + node.status">{{ explanationStatusText(node.status) }}</span></header>
                          <p v-if="nodeSummary(node)">{{ nodeSummary(node) }}</p><small v-if="node.file">{{ node.file }}{{ node.line_start ? ':' + node.line_start : '' }}</small>
                        </article>
                      </details>
                    </div>
                    <p v-else-if="!explanationState(item).loading" class="muted">{{ t('尚未生成该端点的解释快照。') }}</p>
                    <div v-if="explanationState(item).showSnapshots" class="snapshot-list" data-testid="explanation-snapshots">
                      <h5>{{ t('解释快照') }}</h5><p v-if="!explanationState(item).snapshots.length" class="muted">{{ t('暂无快照。') }}</p>
                      <article v-for="snapshot in explanationState(item).snapshots" :key="snapshot.id" class="snapshot-item">
                        <div><b>{{ snapshot.id }}</b><span class="explanation-status" :class="'explanation-' + snapshot.status">{{ explanationStatusText(snapshot.status) }}</span><small>{{ formatSnapshotTime(snapshot.created_at) }}<template v-if="snapshot.model_id || snapshot.model"> · {{ snapshot.model_id || snapshot.model }}</template></small></div>
                        <button v-if="!['running', 'pending'].includes(snapshot.status)" class="secondary sm" type="button" :disabled="explanationState(item).deleting === snapshot.id" @click.stop="deleteExplanationSnapshot(item, snapshot)">{{ t('删除') }}</button>
                      </article>
                    </div>
                  </section>
                  <div class="business-rule-section">
                    <h4>{{ t('业务描述与规则') }} <span v-if="businessFacts(item)" class="br-source" data-testid="business-rule-source">{{ t('来自 API 契约解释') }}</span></h4>
                    <div v-if="businessFacts(item)" class="br-result">
                      <div class="br-parsed">
                        <p v-if="businessFacts(item).summary" class="br-purpose">{{ businessFacts(item).summary }}</p>
                        <details v-if="businessFacts(item).flow.length" class="br-detail">
                          <summary>{{ t('业务步骤') }}</summary>
                          <ol><li v-for="(s, si) in businessFacts(item).flow" :key="si">{{ s }}</li></ol>
                        </details>
                        <details v-if="businessFacts(item).rules.length" class="br-detail">
                          <summary>{{ t('业务规则') }} ({{ businessFacts(item).rules.length }})</summary>
                          <ul><li v-for="(r, ri) in businessFacts(item).rules" :key="ri">{{ r }}</li></ul>
                        </details>
                        <details v-if="businessFacts(item).effects.length" class="br-detail">
                          <summary>{{ t('副作用') }}</summary>
                          <ul><li v-for="(e, ei) in businessFacts(item).effects" :key="ei">{{ e }}</li></ul>
                        </details>
                      </div>
                    </div>
                    <p v-else class="muted">{{ t('业务描述与规则随 API 功能解释一起生成，请先生成该端点的解释快照。') }}</p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody></table></div>
          <div class="pagination" v-if="endpointPages > 1">
            <button :disabled="endpointPage === 1" @click="endpointPage--">{{ t('上一页') }}</button>
            <span>{{ t('第 {page} / {pages} 页', { page: endpointPage, pages: endpointPages }) }}</span>
            <button :disabled="endpointPage === endpointPages" @click="endpointPage++">{{ t('下一页') }}</button>
          </div>
        </template>

        <template v-else-if="activeSection === 'adr'">
          <section class="adr-workbench" data-testid="adr-workbench">
            <section class="adr-card" data-testid="adr-generate">
              <div class="adr-card-heading">
                <div>
                  <h3>{{ t('生成 ADR') }}</h3>
                  <p>{{ t('按来源调用模型生成 MADR 4.0 草稿；草稿保存在本地数据库，导出后才会写入仓库 docs/adr。') }}</p>
                </div>
              </div>
              <div class="adr-tabs" role="tablist">
                <button
                  v-for="tab in adrSourceTabs"
                  :key="tab.key"
                  type="button"
                  role="tab"
                  class="adr-tab"
                  :class="{ active: adrSourceKind === tab.key }"
                  :aria-selected="adrSourceKind === tab.key"
                  :data-testid="`adr-tab-${tab.key}`"
                  @click="adrSourceKind = tab.key"
                >{{ tab.label }}</button>
              </div>
              <div class="adr-form">
                <template v-if="adrSourceKind === 'commit'">
                  <div class="adr-commit-toolbar">
                    <button class="secondary sm" type="button" :disabled="adrScanRunning || adrCommitsLoading" data-testid="adr-commit-scan" @click="scanAdrCommits">{{ adrScanRunning ? t('AI 分诊中...') : t('AI 分诊本页') }}</button>
                    <button class="secondary sm" type="button" :disabled="adrCommitsLoading || adrScanRunning" data-testid="adr-commit-refresh" @click="loadAdrCommits">{{ adrCommitsLoading ? t('加载中...') : t('刷新') }}</button>
                    <label class="adr-check"><input v-model="adrCommitFilterCandidates" type="checkbox" data-testid="adr-commit-filter" /> <span>{{ t('仅显示候选') }}</span></label>
                    <label class="adr-check"><input v-model="adrCommitIncludeMerges" type="checkbox" data-testid="adr-commit-include-merges" @change="resetAdrCommitPaging" /> <span>{{ t('包含合并提交') }}</span></label>
                    <label class="adr-check"><input v-model="adrForce" type="checkbox" data-testid="adr-commit-force" /> <span>{{ t('强制生成已跳过提交') }}</span></label>
                    <span class="adr-commit-summary">{{ t('本页候选 {candidates} / 共 {total} 个提交', { candidates: adrCommitCandidateCount, total: adrCommitsTotalAll }) }}<template v-if="adrCommitsScanned"> · {{ t('已分诊 {scanned}', { scanned: adrCommitsScanned }) }}</template></span>
                    <button class="secondary sm" type="button" :disabled="!adrPendingTotal || adrCommitsLoading" data-testid="adr-select-all-pending" @click="selectAllPendingCommits">{{ t('全选待生成（{count}）', { count: adrPendingTotal }) }}</button>
                    <button class="primary sm" type="button" :disabled="!adrSelectedCommitShas.length || adrBatchSubmitting || adrBatchActive || adrActiveGeneration || adrSubmitting" data-testid="adr-batch-generate" @click="submitAdrBatchGeneration">{{ adrBatchSubmitting ? t('提交中...') : t('生成选中（{count}）', { count: adrSelectedCommitShas.length }) }}</button>
                  </div>
                  <p v-if="adrCommitSelectionNote" class="prompt-hint" data-testid="adr-commit-selection-note">{{ adrCommitSelectionNote }}</p>
                  <div class="adr-commit-progress" data-testid="adr-commit-progress">
                    <div class="llm-progress-heading">
                      <span>{{ t('ADR 提取进展') }}</span>
                      <strong>{{ t('已处理 {processed}/{total}（{percent}%）', { processed: adrCommitsProgress.processed, total: adrCommitsProgress.total, percent: adrCommitsProgress.percent }) }}</strong>
                    </div>
                    <div class="llm-progress-track"><span :style="{ width: `${adrCommitsProgress.percent}%` }"></span></div>
                    <p class="prompt-hint">{{ t('待生成 {pending} · 生成中 {active} · 已处理 {processed}', { pending: adrCommitsPendingCount, active: adrCommitsProgress.active, processed: adrCommitsProgress.processed }) }}</p>
                  </div>
                  <div class="adr-commit-filters">
                    <label class="adr-field"><span>{{ t('起始日期（since）') }}</span><input v-model.trim="adrSince" type="date" data-testid="adr-commit-since" @change="resetAdrCommitPaging" /></label>
                    <label class="adr-field"><span>{{ t('结束日期（until）') }}</span><input v-model.trim="adrUntil" type="date" data-testid="adr-commit-until" @change="resetAdrCommitPaging" /></label>
                  </div>
                  <div class="adr-commit-filters" data-testid="adr-commit-column-filters">
                    <label class="adr-field"><span>{{ t('状态') }}</span>
                      <select :value="adrStageFilter" data-testid="adr-commit-stage-filter" @change="filterAdrCommitsByStage($event.target.value)">
                        <option v-for="option in adrStageOptions" :key="option.key || 'all'" :value="option.key">{{ option.label }}</option>
                      </select>
                    </label>
                    <label class="adr-field"><span>{{ t('候选判定') }}</span>
                      <select v-model="adrCandidateFilter" data-testid="adr-commit-candidate-filter" @change="resetAdrCommitPaging">
                        <option value="">{{ t('全部') }}</option>
                        <option value="candidate">{{ t('候选') }}</option>
                        <option value="rule">{{ t('规则候选') }}</option>
                        <option value="skipped">{{ t('跳过') }}</option>
                      </select>
                    </label>
                    <label class="adr-field"><span>{{ t('跳过原因') }}</span>
                      <select v-model="adrReasonFilter" data-testid="adr-commit-reason-filter" @change="resetAdrCommitPaging">
                        <option value="">{{ t('全部') }}</option>
                        <option value="merge">{{ t('合并提交') }}</option>
                        <option value="noise">{{ t('文档/测试/lockfile') }}</option>
                        <option value="ai">{{ t('AI 分诊跳过') }}</option>
                        <option value="model">{{ t('模型判定非决策') }}</option>
                        <option value="other">{{ t('其他') }}</option>
                      </select>
                    </label>
                  </div>
                  <p class="prompt-hint">{{ t('提交表格覆盖完整 git 历史；合并提交保留在表中并标记为已跳过，其余提交由规则过滤与 AI 分诊判定。勾选候选提交后批量生成，已跳过的提交需勾选“强制生成已跳过提交”。') }}</p>
                  <div class="adr-stage-filters" role="group" :aria-label="t('按状态过滤提交')" data-testid="adr-stage-filters">
                    <button
                      v-for="option in adrStageOptions"
                      :key="option.key || 'all'"
                      type="button"
                      class="adr-stage-chip"
                      :class="{ active: adrStageFilter === option.key }"
                      :aria-pressed="adrStageFilter === option.key"
                      :data-testid="`adr-stage-filter-${option.key || 'all'}`"
                      @click="filterAdrCommitsByStage(option.key)"
                    ><span>{{ option.label }}</span><span class="adr-stage-chip-count">{{ option.count }}</span></button>
                  </div>
                  <p v-if="adrCommitsError" class="explanation-error" data-testid="adr-commits-error">{{ adrCommitsError }}</p>
                  <div v-if="adrVisibleCommits.length" class="table-wrap">
                    <table data-testid="adr-commit-table">
                      <thead><tr>
                        <th class="adr-select-cell"><input type="checkbox" :checked="adrAllCandidatesSelected" :disabled="!adrCandidateShas.length" data-testid="adr-select-all-candidates" @change="toggleAllAdrCandidates" /></th>
                        <th>{{ t('提交') }}</th><th>{{ t('说明') }}</th><th>{{ t('作者') }}</th><th :aria-sort="adrCommitOrder === 'desc' ? 'descending' : 'ascending'"><button class="adr-sort-button" type="button" data-testid="adr-commit-sort-date" :title="adrCommitOrder === 'desc' ? t('当前按日期倒序，点击切换为正序') : t('当前按日期正序，点击切换为倒序')" @click="toggleAdrCommitOrder">{{ t('日期') }} {{ adrCommitOrder === 'desc' ? '↓' : '↑' }}</button></th><th>{{ t('文件数') }}</th><th>{{ t('状态') }}</th><th>{{ t('候选判定') }}</th><th>{{ t('跳过原因') }}</th><th>{{ t('已生成草稿') }}</th><th>{{ t('操作') }}</th>
                      </tr></thead>
                      <tbody>
                        <tr v-for="commit in adrVisibleCommits" :key="commit.sha" :data-testid="`adr-commit-row-${commit.sha}`" :class="{ skipped: !commit.candidate }">
                          <td class="adr-select-cell">
                            <input v-model="adrSelectedCommitShas" type="checkbox" :value="commit.sha" :disabled="!adrCommitSelectable(commit)" :data-testid="`adr-commit-select-${commit.sha}`" />
                          </td>
                          <td><code :title="commit.sha" class="adr-commit-sha">{{ shortAdrCommitSha(commit.sha) }}</code></td>
                          <td class="adr-commit-subject">{{ commit.subject || '-' }}</td>
                          <td>{{ commit.author || '-' }}</td>
                          <td>{{ formatSnapshotTime(commit.committed_at) }}</td>
                          <td :title="commit.is_merge ? t('合并提交不展开文件列表') : (commit.files || []).join('\n')">{{ commit.is_merge ? '—' : (commit.files || []).length }}</td>
                          <td :data-testid="`adr-commit-stage-${commit.sha}`"><span class="term-status adr-stage-badge" :class="`adr-stage-${adrCommitStageKey(commit.stage)}`" :title="adrCommitStageTitle(commit)">{{ adrCommitStageText(commit) }}</span></td>
                          <td>
                            <span class="term-status" :class="commit.candidate ? 'accepted' : 'rejected'" :title="adrCommitCandidateTitle(commit)">{{ adrCommitCandidateText(commit) }}</span>
                            <small v-if="commit.overridden" class="adr-change-type" :title="commit.override_reason || t('人工驳回跳过')">{{ t('人工驳回') }}</small>
                            <small v-else-if="commit.change_type" class="adr-change-type">{{ adrChangeTypeText(commit.change_type) }}</small>
                          </td>
                          <td>
                            <span v-if="commit.is_merge" class="muted">{{ t('合并提交，不生成 ADR') }}</span>
                            <span v-else-if="commit.generation_skip_reason && !commit.draft_id && !commit.overridden" class="muted">{{ commit.generation_skip_reason }}</span>
                            <template v-else-if="!commit.scanned">
                              <span class="muted">{{ t('待 AI 分诊') }}</span>
                              <span v-if="commit.skip_reason" class="muted"> · {{ commit.skip_reason }}</span>
                            </template>
                            <span v-else-if="commit.candidate" class="muted">-</span>
                            <span v-else>{{ commit.skip_reason || '-' }}</span>
                          </td>
                          <td>
                            <template v-if="commit.draft_id">
                              <b>{{ commit.draft_title || commit.draft_id }}</b>
                              <small class="adr-review-chip" :class="commit.review_status || 'pending'">{{ adrReviewStatusText(commit.review_status) }}</small>
                            </template>
                            <span v-else class="muted">-</span>
                          </td>
                          <td class="adr-row-actions">
                            <button class="secondary sm" type="button" :disabled="adrCommitGenerateDisabled(commit)" :title="adrCommitGenerateDisabled(commit) ? adrCommitStageText(commit) : ''" :data-testid="`adr-commit-generate-${commit.sha}`" @click="generateAdrCommit(commit)">{{ adrCommitGenerateLabel(commit) }}</button>
                            <button
                              v-if="adrCommitRejectable(commit)"
                              class="secondary sm"
                              type="button"
                              :disabled="adrOverridingSha === commit.sha || adrBatchActive"
                              :title="t('驳回自动跳过判定，把该提交恢复为候选后重新生成')"
                              :data-testid="`adr-commit-reject-${commit.sha}`"
                              @click="overrideAdrCommit(commit)"
                            >{{ adrOverridingSha === commit.sha ? t('处理中...') : t('驳回') }}</button>
                            <button
                              v-else-if="commit.overridden"
                              class="secondary sm"
                              type="button"
                              :disabled="adrOverridingSha === commit.sha || adrBatchActive"
                              :title="t('恢复自动跳过判定')"
                              :data-testid="`adr-commit-restore-${commit.sha}`"
                              @click="overrideAdrCommit(commit, true)"
                            >{{ adrOverridingSha === commit.sha ? t('处理中...') : t('恢复跳过') }}</button>
                            <button v-if="commit.draft_id" class="secondary sm" type="button" :data-testid="`adr-commit-view-${commit.sha}`" @click="viewAdrCommit(commit)">{{ t('查看') }}</button>
                            <button v-if="commit.draft_id" class="secondary sm" type="button" :disabled="!adrCommitEditable(commit) || adrEditSaving" :title="adrCommitEditable(commit) ? '' : t('已导出的 ADR 不能编辑；如需修改请重新生成草稿')" :data-testid="`adr-commit-edit-${commit.sha}`" @click="editAdrCommit(commit)">{{ t('编辑') }}</button>
                            <button class="secondary sm" type="button" :disabled="adrCommitBacktestDisabled(commit)" :data-testid="`adr-commit-backtest-${commit.sha}`" @click="backtestAdrCommit(commit)">{{ t('回测') }}</button>
                            <button class="secondary sm" type="button" :disabled="adrCommitReviewDisabled(commit)" :data-testid="`adr-commit-review-${commit.sha}`" @click="reviewAdrCommit(commit)">{{ t('审批') }}</button>
                            <button class="secondary sm" type="button" :disabled="adrCommitExportDisabled(commit)" :title="adrCommitExportTitle(commit)" :data-testid="`adr-commit-export-${commit.sha}`" @click="exportAdrCommit(commit)">{{ t('导出') }}</button>
                            <button v-if="commit.draft_id" class="secondary sm danger" type="button" :disabled="adrDeletingId === commit.draft_id" :data-testid="`adr-commit-delete-${commit.sha}`" @click="deleteAdrCommit(commit)">{{ adrDeletingId === commit.draft_id ? t('删除中...') : t('删除') }}</button>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <UiState v-else-if="!adrCommitsLoading && !adrCommitsError && !adrCommits.length" kind="empty" :title="t('没有可展示的提交')" :message="t('调整过滤条件或点击“刷新”重新读取提交列表。')" />
                  <p v-else-if="!adrCommitsLoading && !adrCommitsError" class="muted" data-testid="adr-commits-filtered-out">{{ t('本页没有候选提交，可关闭“仅显示候选”或翻到下一页。') }}</p>
                  <p v-if="adrCommitsLoading && !adrCommits.length" class="muted">{{ t('正在加载提交列表') }}</p>
                  <div v-if="adrCommitsTotal || adrCommitsTotalAll" class="adr-commit-pager" data-testid="adr-commit-pager">
                    <button class="secondary sm" type="button" :disabled="adrCommitPage <= 1 || adrCommitsLoading" data-testid="adr-commit-prev" @click="goAdrCommitPage(adrCommitPage - 1)">{{ t('上一页') }}</button>
                    <button
                      v-for="page in adrCommitPageNumbers"
                      :key="page"
                      type="button"
                      class="secondary sm adr-page-number"
                      :class="{ active: page === adrCommitPage }"
                      :aria-current="page === adrCommitPage ? 'page' : undefined"
                      :aria-label="t('第 {page} 页', { page })"
                      :data-testid="`adr-commit-page-${page}`"
                      @click="goAdrCommitPage(page)"
                    >{{ page }}</button>
                    <span class="adr-commit-pager-info">{{ t('第 {page} / {pages} 页', { page: adrCommitPage, pages: adrCommitPageCount }) }}</span>
                    <button class="secondary sm" type="button" :disabled="adrCommitPage >= adrCommitPageCount || adrCommitsLoading" data-testid="adr-commit-next" @click="goAdrCommitPage(adrCommitPage + 1)">{{ t('下一页') }}</button>
                    <label class="adr-check">{{ t('每页') }}
                      <select :value="adrCommitPageSize" data-testid="adr-commit-page-size" @change="setAdrCommitPageSize($event.target.value)">
                        <option :value="20">20</option>
                        <option :value="50">50</option>
                        <option :value="100">100</option>
                      </select>
                    </label>
                    <span class="muted">{{ t('共 {total} 条', { total: adrCommitsTotal }) }}</span>
                  </div>
                </template>
                <template v-else-if="adrSourceKind === 'file'">
                  <label class="adr-field"><span>{{ t('仓库内文件路径') }}</span><input v-model.trim="adrFilePath" type="text" :placeholder="t('例如 src/app/main.py')" data-testid="adr-file-path" /></label>
                  <label class="adr-field"><span>{{ t('或选择本地文件') }}</span><input type="file" accept=".md,.txt,.py,.ts,.js,.json,.yaml,.yml,.java,.go,.rs" data-testid="adr-file-picker" @change="onAdrFilePicked" /></label>
                  <p v-if="adrPickedFile" class="adr-file-note">{{ t('已选择本地文件：{name}', { name: adrPickedFile.name }) }} <button class="link-button" type="button" @click="clearAdrPickedFile">{{ t('清除') }}</button></p>
                  <p class="prompt-hint">{{ t('填写仓库内路径时由后端读取文件；选择本地文件时直接提交文件内容，文件名保留在来源中。') }}</p>
                </template>
                <template v-else-if="adrSourceKind === 'text'">
                  <label class="adr-field"><span>{{ t('一句话决策') }}</span><textarea v-model="adrText" rows="3" :placeholder="t('例如：网关从 Node 迁移到 Rust，以降低尾延迟')" data-testid="adr-text-input"></textarea></label>
                </template>
                <template v-else>
                  <div class="adr-grid">
                    <label class="adr-field"><span>{{ t('起始日期（since）') }}</span><input v-model.trim="adrSince" type="date" data-testid="adr-since" /></label>
                    <label class="adr-field"><span>{{ t('结束日期（until）') }}</span><input v-model.trim="adrUntil" type="date" data-testid="adr-until" /></label>
                    <label class="adr-field"><span>{{ t('生成数量上限') }}</span><input v-model.number="adrLimit" type="number" min="1" max="50" data-testid="adr-limit" /></label>
                  </div>
                  <label class="adr-check"><input v-model="adrIncludeMerges" type="checkbox" /> <span>{{ t('包含合并提交') }}</span></label>
                  <label class="adr-check"><input v-model="adrForce" type="checkbox" /> <span>{{ t('绕过过滤（force）') }}</span></label>
                  <p class="prompt-hint">{{ t('日期留空表示不限；数量上限按最新提交截取。') }}</p>
                </template>
              </div>
              <div class="adr-actions" v-if="adrSourceKind !== 'commit'">
                <button v-if="adrSourceKind === 'history'" class="primary" :disabled="adrSubmitting || adrBatchActive" data-testid="adr-history-submit" @click="submitAdrHistory">{{ adrSubmitting ? t('提交中...') : t('按历史批量生成') }}</button>
                <button v-else class="primary" :disabled="adrSubmitting || adrActiveGeneration" data-testid="adr-generate-submit" @click="submitAdrGeneration">{{ adrSubmitting || adrActiveGeneration ? t('生成中...') : t('生成') }}</button>
              </div>
              <p v-if="adrError" class="explanation-error" data-testid="adr-generation-error">{{ adrError }}</p>
              <div class="llm-progress" v-if="adrActiveGeneration" data-testid="adr-generation-progress">
                <div class="llm-progress-heading"><span>{{ t('ADR 生成中') }}</span><strong>{{ adrGeneration.progress || 0 }}%</strong></div>
                <div class="llm-progress-track"><span :style="{ width: `${adrGeneration.progress || 0}%` }"></span></div>
                <p>{{ t('状态：{status}', { status: adrGenerationStatusText(adrGeneration.status) }) }} · {{ t('当前阶段：{stage}', { stage: adrStageText(adrGeneration.stage) }) }}</p>
              </div>
              <div class="llm-error" v-if="adrGeneration?.status === 'failed'" data-testid="adr-generation-failed">
                {{ t('ADR 生成失败') }}：{{ adrGeneration.error_message || t('未知错误') }}
              </div>
              <div class="notice" v-if="adrGeneration?.status === 'skipped'" data-testid="adr-generation-skipped">
                {{ t('已跳过本次生成') }}：{{ adrGeneration.skip_reason || t('未说明原因') }}
              </div>
              <p class="adr-success" v-if="adrSuccessNote" data-testid="adr-generation-success">
                {{ adrSuccessNote }}
                <button v-if="adrSuccessDraftId" class="link-button" type="button" @click="openAdrDraft({ id: adrSuccessDraftId })">{{ t('查看草稿') }}</button>
              </p>
              <div class="llm-progress" v-if="adrBatchId" data-testid="adr-batch-progress">
                <div class="llm-progress-heading">
                  <span>{{ t('批量生成进度（已选 {total} 个提交）', { total: adrBatchProgress.total }) }}</span>
                  <strong>{{ t('已完成 {completed}/{total}（{percent}%）', { completed: adrBatchProgress.finished, total: adrBatchProgress.total, percent: adrBatchProgress.percent }) }}</strong>
                </div>
                <div class="llm-progress-track"><span :style="{ width: `${adrBatchProgress.percent}%` }"></span></div>
                <p>{{ t('排队 {queued} · 进行中 {running} · 成功 {succeeded} · 跳过 {skipped} · 失败 {failed}', adrBatchCounts) }}</p>
                <p v-if="adrBatchNote" class="adr-batch-note">{{ adrBatchNote }}</p>
              </div>
              <p v-if="adrExportState.path" class="adr-success" data-testid="adr-export-result">{{ t('已导出到 {path}', { path: adrExportState.path }) }}</p>
              <p v-if="adrExportState.error" class="explanation-error">{{ t('导出失败') }}：{{ adrExportState.error }}</p>
            </section>

            <section v-if="adrOtherDrafts.length || adrDraftsError" class="adr-card" data-testid="adr-other-drafts">
              <div class="adr-card-heading">
                <div>
                  <h3>{{ t('其他来源草稿') }}</h3>
                  <p>{{ t('来自文件、一句话与历史生成的草稿；提交来源的草稿直接在提交表格的操作列查看、编辑与导出。') }}</p>
                </div>
                <button class="secondary sm" :disabled="adrDraftsLoading" @click="loadAdrDrafts">{{ adrDraftsLoading ? t('加载中...') : t('刷新') }}</button>
              </div>
              <p v-if="adrDraftsError" class="explanation-error">{{ adrDraftsError }}</p>
              <div v-if="adrOtherDrafts.length" class="table-wrap">
                <table data-testid="adr-other-drafts-table">
                  <thead><tr><th>{{ t('标题') }}</th><th>{{ t('状态') }}</th><th>{{ t('来源') }}</th><th>{{ t('生成时间') }}</th><th>{{ t('回测') }}</th><th>{{ t('审批') }}</th><th>{{ t('操作') }}</th></tr></thead>
                  <tbody>
                    <tr v-for="draft in adrOtherDrafts" :key="draft.id" :class="{ selected: adrDetail?.draft?.id === draft.id }">
                      <td>
                        <b>{{ draft.title || draft.slug || draft.id }}</b>
                        <small v-if="draft.revision > 1" class="adr-version">{{ t('版本 v{revision}', { revision: draft.revision }) }}</small>
                        <small v-if="draft.exported_path" class="adr-exported">{{ t('已导出到 {path}', { path: draft.exported_path }) }}</small>
                      </td>
                      <td><span class="term-status" :class="adrDraftStatusKey(draft.status)">{{ adrDraftStatusText(draft.status) }}</span></td>
                      <td>{{ adrSourceKindLabel(draft.source_kind) }} · <code>{{ shortAdrRef(draft.source_ref) }}</code></td>
                      <td>{{ formatSnapshotTime(draft.created_at) }}</td>
                      <td><span class="adr-backtest-summary" :class="adrBacktestVerdictKey(draft.backtest?.verdict)" :title="draft.backtest?.verdict || draft.backtest?.status || ''">{{ adrBacktestSummary(draft.backtest) }}</span></td>
                      <td><span class="adr-review-chip" :class="draft.review_status || 'pending'" :title="draft.review_status || 'pending'">{{ adrReviewStatusText(draft.review_status) }}</span></td>
                      <td class="adr-row-actions">
                        <button class="secondary sm" :disabled="adrDetailLoading" @click="openAdrDraft(draft)">{{ t('查看') }}</button>
                        <button class="secondary sm" :disabled="!adrDraftEditable(draft) || adrEditSaving" :title="adrDraftEditable(draft) ? '' : t('已导出的 ADR 不能编辑；如需修改请重新生成草稿')" :data-testid="`adr-draft-edit-${draft.id}`" @click="openAdrDraft(draft, { edit: true })">{{ t('编辑') }}</button>
                        <button class="secondary sm" :disabled="adrBacktestingId === draft.id" :data-testid="`adr-draft-backtest-${draft.id}`" @click="runAdrBacktestForDraft(draft)">{{ adrBacktestingId === draft.id ? t('回测中...') : t('回测') }}</button>
                        <button class="secondary sm" :data-testid="`adr-draft-review-${draft.id}`" @click="openAdrDraftForReview(draft)">{{ t('审批') }}</button>
                        <button class="secondary sm" :disabled="!adrDraftExportable(draft) || adrExportingId === draft.id" :title="adrDraftExportable(draft) ? '' : t('需人工审批通过后才能导出')" :data-testid="`adr-draft-export-${draft.id}`" @click="exportAdrDraft(draft)">{{ adrExportingId === draft.id ? t('导出中...') : t('导出') }}</button>
                        <button class="secondary sm danger" :disabled="adrDeletingId === draft.id" @click="deleteAdrDraft(draft)">{{ adrDeletingId === draft.id ? t('删除中...') : t('删除') }}</button>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>

            <section v-if="adrDetail || adrDetailLoading || adrDetailError" class="adr-detail" data-testid="adr-detail">
                <div class="adr-detail-heading">
                  <h4>{{ t('ADR 草稿详情') }}<template v-if="adrDetail?.draft?.title"> · {{ adrDetail.draft.title }}</template></h4>
                  <button class="secondary sm" type="button" @click="closeAdrDetail">{{ t('关闭') }}</button>
                </div>
                <p v-if="adrDetailLoading" class="muted">{{ t('正在加载 ADR 草稿详情...') }}</p>
                <p v-else-if="adrDetailError" class="explanation-error">{{ adrDetailError }}</p>
                <template v-else-if="adrDetail">
                  <div class="adr-detail-meta">
                    <span>{{ t('状态：{status}', { status: adrDraftStatusText(adrDetail.draft?.status) }) }}</span>
                    <span>{{ t('审批：{status}', { status: adrReviewStatusText(adrDetail.draft?.review_status) }) }}</span>
                    <span v-if="adrDetail.draft?.revision">{{ t('版本 v{revision}', { revision: adrDetail.draft.revision }) }}</span>
                    <span v-if="adrDetail.draft?.editor">{{ t('编辑人：{editor}', { editor: adrDetail.draft.editor }) }}</span>
                    <span>{{ t('生成时间：{time}', { time: formatSnapshotTime(adrDetail.draft?.created_at) }) }}</span>
                    <span v-if="adrDetail.draft?.exported_path">{{ t('已导出到 {path}', { path: adrDetail.draft.exported_path }) }}</span>
                  </div>
                  <div class="adr-detail-actions">
                    <button class="secondary sm" type="button" :disabled="!adrDraftEditable(adrDetail.draft) || adrEditSaving" :title="adrDraftEditable(adrDetail.draft) ? '' : t('已导出的 ADR 不能编辑；如需修改请重新生成草稿')" data-testid="adr-detail-edit" @click="startAdrEdit(adrDetail.draft)">{{ t('编辑') }}</button>
                    <span v-if="!adrDraftExportable(adrDetail.draft)" class="prompt-hint">{{ t('需人工审批通过后才能导出') }}</span>
                    <button class="secondary sm" type="button" :disabled="!adrDraftExportable(adrDetail.draft) || adrExportingId === adrDetail.draft?.id" :title="adrDraftExportable(adrDetail.draft) ? '' : t('需人工审批通过后才能导出')" data-testid="adr-detail-export" @click="exportAdrDraft(adrDetail.draft)">{{ adrExportingId === adrDetail.draft?.id ? t('导出中...') : t('导出') }}</button>
                  </div>
                  <h5>{{ t('MADR 草稿内容') }}</h5>
                  <section v-if="adrEditing" class="adr-detail-section" data-testid="adr-editor">
                    <label class="adr-field"><span>{{ t('ADR 内容（Markdown）') }}</span><textarea v-model="adrEditMarkdown" rows="12" data-testid="adr-edit-markdown"></textarea></label>
                    <div class="adr-grid">
                      <label class="adr-field"><span>{{ t('标题（可选）') }}</span><input v-model.trim="adrEditTitle" type="text" :placeholder="adrDetail.draft?.title || ''" data-testid="adr-edit-title" /></label>
                      <label class="adr-field"><span>{{ t('编辑人（可选）') }}</span><input v-model.trim="adrEditEditor" type="text" :placeholder="t('可选：编辑人姓名')" data-testid="adr-edit-editor" /></label>
                    </div>
                    <label class="adr-field"><span>{{ t('修订说明（可选）') }}</span><input v-model.trim="adrEditNote" type="text" :placeholder="t('例如：精简背景描述、补充后果')" data-testid="adr-edit-note" /></label>
                    <div class="adr-actions">
                      <button class="primary sm" type="button" :disabled="adrEditSaving || !adrEditMarkdown.trim()" data-testid="adr-edit-save" @click="saveAdrEdit">{{ adrEditSaving ? t('保存中...') : t('保存为新版本') }}</button>
                      <button class="secondary sm" type="button" :disabled="adrEditSaving" data-testid="adr-edit-cancel" @click="cancelAdrEdit">{{ t('取消') }}</button>
                    </div>
                    <p class="prompt-hint">{{ t('保存后成为新版本，审批状态重置为待审批，回测结果失效。') }}</p>
                    <p v-if="adrEditError" class="explanation-error" data-testid="adr-edit-error">{{ adrEditError }}</p>
                  </section>
                  <pre v-else class="adr-markdown">{{ adrDetail.draft?.markdown || '' }}</pre>

                  <section v-if="(adrDetail.revisions || []).length" class="adr-detail-section" data-testid="adr-revisions">
                    <h5>{{ t('版本历史') }}</h5>
                    <ul class="adr-revisions">
                      <li v-for="revision in adrDetail.revisions" :key="revision.id">
                        <b>{{ t('版本 v{revision}', { revision: revision.revision }) }}</b>
                        <span class="muted">{{ formatSnapshotTime(revision.created_at) }}</span>
                        <span v-if="revision.editor">{{ t('编辑人：{editor}', { editor: revision.editor }) }}</span>
                        <span v-if="revision.note" class="muted">{{ revision.note }}</span>
                        <button class="link-button" type="button" @click="previewAdrRevision(revision)">{{ t('查看') }}</button>
                      </li>
                    </ul>
                    <pre v-if="adrRevisionPreview" class="adr-markdown" data-testid="adr-revision-preview">{{ adrRevisionPreview.markdown }}</pre>
                  </section>

                  <section class="adr-detail-section" data-testid="adr-detail-backtest">
                    <div class="adr-section-heading">
                      <h5>{{ t('代码回测') }}</h5>
                      <button class="secondary sm" type="button" :disabled="adrBacktestingId === adrDetail.draft?.id" data-testid="adr-detail-backtest-run" @click="runAdrBacktest">{{ adrBacktestingId === adrDetail.draft?.id ? t('回测中...') : (adrDetail.backtest ? t('重新回测') : t('运行回测')) }}</button>
                    </div>
                    <p v-if="adrBacktestError" class="explanation-error">{{ adrBacktestError }}</p>
                    <template v-if="adrDetail.backtest">
                      <p class="adr-backtest-verdict">
                        <span class="adr-backtest-summary" :class="adrBacktestVerdictKey(adrDetail.backtest.verdict)">{{ adrBacktestVerdictText(adrDetail.backtest.verdict) }}</span>
                        <span>{{ t('置信度 {percent}%', { percent: adrBacktestConfidencePercent(adrDetail.backtest) }) }}</span>
                        <span v-if="adrBacktestStatusText(adrDetail.backtest.status)">{{ adrBacktestStatusText(adrDetail.backtest.status) }}</span>
                      </p>
                      <div class="adr-confidence-track"><span :style="{ width: `${adrBacktestConfidencePercent(adrDetail.backtest)}%` }"></span></div>
                      <p v-if="adrDetail.backtest.error_message" class="explanation-error">{{ adrDetail.backtest.error_message }}</p>
                      <template v-if="(adrDetail.backtest.reasons || []).length">
                        <h6>{{ t('回测理由') }}</h6>
                        <ul class="adr-reasons"><li v-for="(reason, index) in adrDetail.backtest.reasons" :key="index">{{ reason }}</li></ul>
                      </template>
                      <template v-if="(adrDetail.backtest.evidence || []).length">
                        <h6>{{ t('代码证据') }}</h6>
                        <div class="table-wrap"><table data-testid="adr-backtest-evidence"><thead><tr><th>{{ t('文件') }}</th><th>{{ t('行号') }}</th><th>{{ t('说明') }}</th></tr></thead><tbody>
                          <tr v-for="(item, index) in adrDetail.backtest.evidence" :key="index">
                            <td><code>{{ item.path || '-' }}</code></td>
                            <td>{{ item.line ?? '-' }}</td>
                            <td>{{ item.note || '-' }}</td>
                          </tr>
                        </tbody></table></div>
                      </template>
                      <template v-if="adrDetail.backtest.recommendation">
                        <h6>{{ t('建议') }}</h6>
                        <p>{{ adrDetail.backtest.recommendation }}</p>
                      </template>
                      <p class="muted">{{ t('回测时间：{time}', { time: formatSnapshotTime(adrDetail.backtest.created_at) }) }}<template v-if="adrDetail.backtest.duration_ms != null"> · {{ adrDetail.backtest.duration_ms }} ms</template></p>
                    </template>
                    <p v-else class="muted">{{ t('尚未运行回测。') }}</p>
                  </section>

                  <section class="adr-detail-section" data-testid="adr-detail-review">
                    <h5>{{ t('人工审批') }}</h5>
                    <p class="adr-review-status">
                      {{ t('当前审批状态：{status}', { status: adrReviewStatusText(adrDetail.draft?.review_status) }) }}
                      <template v-if="adrDetail.draft?.rating"> · {{ t('评分：{rating} 星', { rating: adrDetail.draft.rating }) }}</template>
                      <template v-if="adrDetail.draft?.reviewed_at"> · {{ t('审批时间：{time}', { time: formatSnapshotTime(adrDetail.draft.reviewed_at) }) }}</template>
                    </p>
                    <p v-if="adrDetail.draft?.review_comment" class="muted">{{ t('审批意见：{comment}', { comment: adrDetail.draft.review_comment }) }}</p>
                    <div class="adr-review-form">
                      <label class="adr-field"><span>{{ t('评分（可选）') }}</span>
                        <select v-model.number="adrReviewRating" data-testid="adr-review-rating">
                          <option :value="null">{{ t('不评分') }}</option>
                          <option v-for="score in [1, 2, 3, 4, 5]" :key="score" :value="score">{{ score }}</option>
                        </select>
                      </label>
                      <label class="adr-field"><span>{{ t('审批人（可选）') }}</span><input v-model.trim="adrReviewReviewer" type="text" :placeholder="t('可选：审批人姓名')" data-testid="adr-review-reviewer" /></label>
                      <label class="adr-field"><span>{{ t('审批意见') }}</span><textarea v-model="adrReviewComment" rows="3" :placeholder="t('可选：补充审批意见')" data-testid="adr-review-comment"></textarea></label>
                    </div>
                    <div class="adr-actions">
                      <button class="primary sm" type="button" :disabled="adrReviewing || adrDetail.draft?.review_status === 'approved'" data-testid="adr-review-approve" @click="submitAdrReview('approve')">{{ adrReviewing ? t('提交中...') : t('通过') }}</button>
                      <button class="secondary sm danger" type="button" :disabled="adrReviewing || adrDetail.draft?.review_status === 'rejected'" data-testid="adr-review-reject" @click="submitAdrReview('reject')">{{ adrReviewing ? t('提交中...') : t('驳回') }}</button>
                    </div>
                    <p v-if="adrReviewNotice" class="adr-success">{{ adrReviewNotice }}</p>
                    <p v-if="adrReviewError" class="explanation-error">{{ adrReviewError }}</p>
                    <p class="prompt-hint">{{ t('草稿需审批通过后才能导出到仓库 docs/adr。') }}</p>
                  </section>

                  <details class="adr-debug" open>
                    <summary>{{ t('调试映射') }}</summary>
                    <template v-if="adrDetail.generation">
                      <h6>{{ t('来源') }}</h6>
                      <p class="muted">
                        {{ t('来源类型：{kind}', { kind: adrSourceKindLabel(adrDetail.generation.source_kind) }) }}
                        · {{ t('来源引用：{ref}', { ref: adrDetail.generation.source_ref || '-' }) }}
                        · {{ t('来源摘要：{digest}', { digest: adrDetail.generation.source_digest || '-' }) }}
                      </p>
                      <details>
                        <summary>{{ t('来源载荷（source_payload）') }}</summary>
                        <pre>{{ formatJson(adrDetail.generation.source_payload || {}) }}</pre>
                      </details>
                      <h6>{{ t('提示词') }}</h6>
                      <p class="muted">
                        {{ t('提示词来源：{origin}', { origin: adrDetail.generation.prompt_id ? t('服务自定义') : t('系统默认') }) }}
                        · {{ t('版本：v{version}', { version: adrDetail.generation.prompt_version ?? 0 }) }}
                        · {{ t('摘要：{digest}', { digest: adrDetail.generation.prompt_digest || '-' }) }}
                      </p>
                      <pre>{{ adrDetail.generation.prompt_text || '-' }}</pre>
                      <h6>{{ t('模型') }}</h6>
                      <p>{{ adrDetail.generation.model || '-' }}</p>
                      <h6>{{ t('原始响应') }}</h6>
                      <pre>{{ adrDetail.generation.raw_response || '-' }}</pre>
                      <h6>{{ t('解析结果') }}</h6>
                      <pre>{{ adrDetail.generation.parsed ? formatJson(adrDetail.generation.parsed) : '-' }}</pre>
                      <h6>{{ t('错误 / 跳过原因') }}</h6>
                      <p>{{ adrDetail.generation.error_message || adrDetail.generation.skip_reason || '-' }}</p>
                      <h6>{{ t('耗时') }}</h6>
                      <p>{{ adrDetail.generation.duration_ms ?? '-' }} ms</p>
                    </template>
                    <p v-else class="muted">{{ t('该草稿没有关联的生成记录。') }}</p>
                  </details>
                </template>
              </section>

            <details class="adr-card adr-prompt-card" data-testid="adr-prompt-settings">
              <summary>{{ t('ADR 提示词 · 按服务') }}</summary>
              <div class="adr-prompt-body">
                <div class="adr-card-heading">
                  <div>
                    <p>{{ t('提示词按服务保存版本，生成时按来源类型解析；未自定义时使用系统默认模板。') }}</p>
                  </div>
                  <span v-if="adrPrompts?.member_id" class="adr-member">{{ t('当前服务') }}：<code>{{ adrPrompts.member_id }}</code></span>
                </div>
                <div class="adr-tabs">
                  <button
                    v-for="tab in adrPromptTabs"
                    :key="tab.key"
                    type="button"
                    class="adr-tab"
                    :class="{ active: adrPromptKind === tab.key }"
                    @click="selectAdrPromptKind(tab.key)"
                  >{{ tab.label }}</button>
                </div>
                <p class="adr-prompt-meta">
                  <span class="term-status" :class="{ accepted: adrPromptIsCustom }">{{ adrPromptVersionLabel }}</span>
                  <span>{{ t('提示词摘要：{digest}', { digest: adrPromptDigestValue || '-' }) }}</span>
                </p>
                <label class="prompt-field">
                  <span>{{ t('生效提示词') }}</span>
                  <textarea v-model="adrPromptText" rows="14" :placeholder="t('留空无法保存；点击“恢复默认”可回到系统模板。')" data-testid="adr-prompt-textarea"></textarea>
                </label>
                <p class="prompt-hint">{{ adrPromptPlaceholderHint }}</p>
                <div class="api-prompt-actions">
                  <button class="secondary sm" :disabled="adrPromptSaving || !adrPromptText.trim()" @click="saveAdrPrompt">{{ adrPromptSaving ? t('保存中...') : t('保存') }}</button>
                  <button class="secondary sm" :disabled="adrPromptSaving || !adrPromptIsCustom" @click="resetAdrPrompt">{{ t('恢复默认') }}</button>
                </div>
                <p v-if="adrPromptNotice" class="adr-success">{{ adrPromptNotice }}</p>
                <p v-if="adrPromptError" class="explanation-error">{{ adrPromptError }}</p>
              </div>
            </details>

            <details class="adr-card adr-prompt-card" data-testid="adr-template-settings">
              <summary>{{ t('ADR 输出模板（MADR 4.0）') }}</summary>
              <div class="adr-prompt-body">
                <div class="adr-card-heading">
                  <div>
                    <p>{{ t('输出模板决定模型结果如何渲染为 MADR 4.0；按服务保存版本，未自定义时使用系统默认模板。') }}</p>
                  </div>
                  <span v-if="adrTemplate?.member_id" class="adr-member">{{ t('当前服务') }}：<code>{{ adrTemplate.member_id }}</code></span>
                </div>
                <p class="adr-prompt-meta">
                  <span class="term-status" :class="{ accepted: adrTemplateIsCustom }">{{ adrTemplateVersionLabel }}</span>
                  <span>{{ t('模板摘要：{digest}', { digest: adrTemplateDigest || '-' }) }}</span>
                </p>
                <label class="prompt-field">
                  <span>{{ t('生效模板') }}</span>
                  <textarea v-model="adrTemplateText" rows="12" :placeholder="t('留空无法保存；点击“恢复默认”可回到系统模板。')" data-testid="adr-template-textarea"></textarea>
                </label>
                <p class="prompt-hint">{{ adrTemplatePlaceholderHint }}</p>
                <div class="api-prompt-actions">
                  <button class="secondary sm" :disabled="adrTemplateSaving || !adrTemplateText.trim()" data-testid="adr-template-save" @click="saveAdrTemplate">{{ adrTemplateSaving ? t('保存中...') : t('保存') }}</button>
                  <button class="secondary sm" :disabled="adrTemplateSaving || !adrTemplateIsCustom" data-testid="adr-template-reset" @click="resetAdrTemplate">{{ t('恢复默认') }}</button>
                </div>
                <p v-if="adrTemplateNotice" class="adr-success">{{ adrTemplateNotice }}</p>
                <p v-if="adrTemplateError" class="explanation-error">{{ adrTemplateError }}</p>
              </div>
            </details>
          </section>

          <section class="adr-facts" data-testid="adr-facts">
            <h3>{{ t('仓库已有 ADR（Snapshot 冻结）') }}</h3>
            <p class="muted">{{ t('以下记录读取自被分析仓库中已提交的 ADR，随 Snapshot 冻结，仅作只读查看；与上方生成的草稿互不影响。') }}</p>
            <UiState
              v-if="!activeData.available"
              kind="empty"
              :title="t('该 Snapshot 未包含 ADR 决策记录')"
              :message="t('ADR 读取自被分析仓库的 docs/adr（或 doc/adr、docs/decisions、adr），需在生成 Snapshot 前提交。')"
            />
            <template v-else>
              <div class="metrics">
                <div class="metric">{{ activeData.total ?? 0 }} <small>{{ t('条决策') }}</small></div>
                <div class="metric">{{ adrStatusCount('accepted') }} <small>{{ t('已接受') }}</small></div>
                <div class="metric">{{ adrStatusCount('proposed') }} <small>{{ t('提议中') }}</small></div>
                <div class="metric" :class="{ danger: (activeData.error_count || 0) > 0 }">{{ activeData.error_count ?? 0 }} <small>{{ t('个校验错误') }}</small></div>
              </div>
              <p class="muted">
                {{ t('目录') }}: <code>{{ activeData.adr_dir }}</code>
                <span v-if="!activeData.index_present"> · {{ t('缺少 index.md 索引') }}</span>
                <span v-else-if="activeData.index_stale"> · {{ t('索引已过期，需运行 adr-index 重建') }}</span>
              </p>
              <div class="table-wrap"><table data-testid="adr-table"><thead><tr>
                <th>{{ t('编号') }}</th><th>{{ t('标题') }}</th><th>{{ t('状态') }}</th><th>{{ t('日期') }}</th><th>{{ t('替代关系') }}</th><th>{{ t('文件') }}</th>
              </tr></thead><tbody>
                <tr v-for="row in activeData.records || []" :key="row.path">
                  <td><b>{{ row.id }}</b></td>
                  <td>{{ row.title || '-' }}</td>
                  <td><span class="term-status" :class="adrStatusKey(row.status)">{{ adrStatusLabel(row.status) }}</span></td>
                  <td>{{ row.date || '-' }}</td>
                  <td>{{ adrSupersedeText(row) }}</td>
                  <td><code>{{ row.path }}</code></td>
                </tr>
              </tbody></table></div>
              <template v-if="(activeData.violations || []).length">
                <h3>{{ t('结构校验') }} ({{ activeData.violations.length }})</h3>
                <div class="table-wrap"><table data-testid="adr-violations"><thead><tr>
                  <th>{{ t('规则') }}</th><th>{{ t('级别') }}</th><th>{{ t('文件') }}</th><th>{{ t('说明') }}</th>
                </tr></thead><tbody>
                  <tr v-for="(issue, index) in activeData.violations" :key="index">
                    <td><code>{{ issue.rule }}</code></td>
                    <td>{{ issue.level === 'error' ? t('错误') : t('警告') }}</td>
                    <td><code>{{ issue.path }}</code></td>
                    <td>{{ issue.message }}</td>
                  </tr>
                </tbody></table></div>
              </template>
            </template>
          </section>
        </template>

        <template v-else-if="activeSection === 'module_topology'">
          <div class="topology-toolbar">
            <span class="source-badge" :class="'source-' + topologySource">{{ t('分类来源：{source}', { source: topologySourceLabel }) }}</span>
            <button v-if="topologySource !== 'llm'" class="primary" :disabled="taxonomyActive" @click="classifyBusiness">
              {{ taxonomyActive ? t('正在用 LLM 分类') : t('按业务分类（LLM）') }}
            </button>
            <template v-else>
              <button class="secondary" :disabled="taxonomyActive" @click="classifyBusiness">{{ t('重新分类') }}</button>
              <button class="secondary" :disabled="taxonomyActive" @click="resetTaxonomy">{{ t('恢复目录分类') }}</button>
            </template>
          </div>
          <div class="llm-progress" v-if="taxonomyActive" data-testid="taxonomy-progress">
            <div class="llm-progress-heading"><span>{{ t('LLM 业务分类进行中') }}</span><strong>{{ taxonomyProgress }}%</strong></div>
            <div class="llm-progress-track"><span :style="{ width: `${taxonomyProgress}%` }"></span></div>
            <p>{{ t('当前阶段：{stage}', { stage: taxonomyStage }) }}</p>
          </div>
          <div class="llm-error" v-if="taxonomyJob?.status === 'failed'">
            {{ t('LLM 业务分类失败') }}：{{ taxonomyJob.error_message || t('未知错误') }}
          </div>
          <div class="llm-note" v-if="taxonomyMeta">
            {{ t('由 {model} 分类，{assigned}/{total} 个文件归入业务域', { model: taxonomyMeta.model, assigned: taxonomyMeta.stats?.assigned_count ?? 0, total: taxonomyMeta.stats?.file_count ?? 0 }) }}
            <span v-if="taxonomyMeta.issues?.length"> · {{ taxonomyMeta.issues.join('；') }}</span>
          </div>
          <div class="notice" v-else-if="topologySource !== 'llm'">
            {{ t('当前按目录骨架展示。文件名不含业务域前缀时，可让 LLM 按业务能力重新分类（一次调用，结果缓存）。') }}
          </div>
          <div class="metrics"><div class="metric" v-for="stat in moduleStats" :key="stat.key">{{ stat.value }} <small>{{ stat.label }}</small></div><div class="metric">{{ activeData.coupling_score ?? '-' }} <small>{{ t('耦合度') }}</small></div></div>
          <div class="module-tree" data-testid="module-tree">
            <p v-if="!moduleRows.length" class="tree-empty">{{ t('暂无模块') }}</p>
            <div
              v-for="row in moduleRows"
              :key="row.key"
              class="tree-row"
              :class="[`tree-${row.type}`, `tree-level-${row.level}`]"
              :style="{ paddingLeft: 10 + row.level * 18 + 'px' }"
            >
              <template v-if="row.type === 'module'">
                <button
                  v-if="row.hasChildren"
                  class="tree-toggle"
                  :aria-label="expandedModules.has(row.node.id) ? t('收起') : t('展开')"
                  @click="toggleModule(row.node)"
                >{{ expandedModules.has(row.node.id) ? '▾' : '▸' }}</button>
                <span v-else class="tree-toggle tree-toggle-empty"></span>
                <button class="tree-label" @click="toggleModule(row.node)">{{ moduleTitle(row.node) }}</button>
                <span v-if="moduleKindLabel(row.node.kind)" class="kind-badge" :class="'kind-' + row.node.kind">{{ moduleKindLabel(row.node.kind) }}</span>
                <span class="tree-meta">{{ row.node.total_file_count ?? row.node.file_count }} {{ t('个文件') }}</span>
                <span v-if="row.node.kind !== 'root' && (row.node.afferent || row.node.efferent)" class="tree-meta">{{ t('被依赖 {afferent} · 依赖 {efferent}', { afferent: row.node.afferent ?? 0, efferent: row.node.efferent ?? 0 }) }}</span>
                <span v-for="layer in moduleLayers(row.node)" :key="layer.name" class="layer-chip">{{ layer.name }} <small>{{ layer.count }}</small></span>
                <span v-if="row.node.layer_conflict" class="layer-conflict">{{ t('{count} 个文件名与目录分层不一致', { count: row.node.layer_conflict }) }}</span>
                <span v-if="row.node.description" class="tree-description">{{ row.node.description }}</span>
              </template>
              <template v-else-if="row.type === 'file'">
                <span class="tree-file-mark">▪</span>
                <code class="tree-file">{{ row.path }}</code>
                <span v-if="evidenceLoading[row.moduleId]" class="tree-meta">{{ t('正在加载证据') }}</span>
                <span v-else-if="row.symbolCount !== null" class="tree-meta">{{ t('{count} 个类/函数', { count: row.symbolCount }) }}</span>
              </template>
              <template v-else>
                <span class="symbol-kind" :class="'sym-' + row.symbol.kind">{{ symbolKindLabel(row.symbol.kind) }}</span>
                <span class="tree-symbol">{{ row.symbol.name }}</span>
              </template>
            </div>
          </div>
        </template>

        <template v-else-if="activeSection === 'core_entities'">
          <div class="table-wrap"><table><thead><tr><th>{{ t('领域对象') }}</th><th>{{ t('类型') }}</th><th>{{ t('字段') }}</th><th>{{ t('关系') }}</th><th>{{ t('领域分') }}</th><th>{{ t('仓库/文件') }}</th></tr></thead><tbody>
            <template v-for="item in activeData || []" :key="item.qualified_name">
              <tr class="clickable" :class="{ expanded: expandedEntityKeys.has(item.node_id || item.qualified_name) }" tabindex="0" @click="toggleEntity(item)" @keydown.enter="toggleEntity(item)">
                <td><b>{{ item.name }}</b></td><td>{{ item.kind }}</td><td>{{ item.field_count }}</td><td>{{ item.relationship_count }}</td><td>{{ Number(item.score || 0).toFixed(2) }}</td><td><span class="repo-badge">{{ item.repository }}</span><code>{{ item.file_path }}</code></td>
              </tr>
              <tr v-if="expandedEntityKeys.has(item.node_id || item.qualified_name)" class="expand-detail">
                <td colspan="6">
                  <div class="entity-detail">
                    <div><strong>{{ t('限定名') }}:</strong> <code>{{ item.qualified_name }}</code></div>
                    <div><strong>{{ t('类型') }}:</strong> {{ item.kind }} &middot; <strong>{{ t('分层') }}:</strong> {{ item.layer || t('未分类') }} &middot; <strong>{{ t('领域分') }}:</strong> {{ Number(item.score || 0).toFixed(2) }}</div>
                    <div><strong>{{ t('文件位置') }}:</strong> <code>{{ item.file_path }}{{ item.start_line ? ':' + item.start_line : '' }}</code></div>
                    <div><strong>{{ t('语言') }}:</strong> {{ item.language || '-' }} &middot; <strong>{{ t('评分构成') }}:</strong> <code>{{ scoreBreakdownText(item.score_breakdown) || '-' }}</code></div>
                    <div v-if="item.annotations?.length"><strong>{{ t('标注') }}:</strong> {{ item.annotations.join(', ') }}</div>
                    <div v-if="item.fields?.length" class="field-list">
                      <strong>{{ t('字段') }} ({{ item.fields.length }}):</strong>
                      <table class="field-table"><thead><tr><th>{{ t('名称') }}</th><th>{{ t('类型') }}</th><th>{{ t('行') }}</th></tr></thead><tbody>
                        <tr v-for="f in item.fields" :key="f.name"><td><code>{{ f.name }}</code></td><td>{{ f.signature || f.kind || '-' }}</td><td>{{ f.start_line || '-' }}</td></tr>
                      </tbody></table>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'layer_violations'">
          <div class="metric danger">{{ activeData.violation_count || 0 }} <small>{{ t('个分层违规') }}</small></div>
          <div class="table-wrap"><table><thead><tr><th>{{ t('来源') }}</th><th>{{ t('依赖') }}</th><th>{{ t('目标') }}</th></tr></thead><tbody><tr v-for="(item, index) in activeData.violations || []" :key="index"><td><b>{{ item.source_layer }}</b><br><code>{{ item.source_file }}</code></td><td>→</td><td><b>{{ item.target_layer }}</b><br><code>{{ item.target_file }}</code></td></tr></tbody></table></div>
          <LayerRules :snapshot-id="snapshotId" />
        </template>

        <template v-else-if="activeSection === 'config_consumption'">
          <div class="metrics">
            <div class="metric">{{ activeData.config_files ?? 0 }} <small>{{ t('个配置文件') }}</small></div>
            <div class="metric">{{ activeData.total_keys ?? 0 }} <small>{{ t('个配置键') }}</small></div>
            <div class="metric">{{ activeData.consumed_keys ?? 0 }} <small>{{ t('个已消费键') }}</small></div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>{{ t('配置文件') }}</th><th>{{ t('键数') }}</th><th>{{ t('已消费') }}</th><th>{{ t('消费者') }}</th></tr></thead><tbody>
            <tr v-for="file in activeData.files || []" :key="file.config_file">
              <td><code>{{ file.config_file }}</code></td>
              <td>{{ file.key_count }}</td>
              <td>{{ file.consumed_keys }}</td>
              <td>
                <div v-for="(use, index) in file.consumers || []" :key="index">
                  <code>{{ use.config_key }}</code> → <b>{{ use.consumer_name }}</b><br><small class="muted">{{ use.consumer_file }}:{{ use.consumer_line }}</small>
                </div>
              </td>
            </tr>
          </tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'interface_contract'">
          <UiState v-if="!activeData.entries?.length" kind="empty" :title="t('该 Snapshot 未识别到公开接口')" :message="t('公开接口指顶层导出函数与类的公开方法，非 HTTP 面。')" />
          <template v-else>
            <div class="metrics">
              <div class="metric">{{ activeData.entry_count ?? 0 }} <small>{{ t('个公开入口') }}</small></div>
              <div class="metric">{{ activeData.file_count ?? 0 }} <small>{{ t('个文件') }}</small></div>
            </div>
            <div class="table-wrap"><table><thead><tr><th>{{ t('名称') }}</th><th>{{ t('类型') }}</th><th>{{ t('签名') }}</th><th>{{ t('语言') }}</th><th>{{ t('文件') }}</th></tr></thead><tbody>
              <tr v-for="row in activeData.entries" :key="row.qualified_name || `${row.name}@${row.file_path}`">
                <td><b>{{ row.name }}</b></td>
                <td>{{ row.kind }}</td>
                <td><code>{{ row.signature || row.qualified_name }}</code></td>
                <td>{{ row.language }}</td>
                <td><code>{{ row.file_path }}:{{ row.line }}</code></td>
              </tr>
            </tbody></table></div>
          </template>
        </template>

        <template v-else-if="activeSection === 'external_dependencies'">
          <div class="metrics">
            <div class="metric">{{ activeData.categories ?? 0 }} <small>{{ t('个分类') }}</small></div>
            <div class="metric">{{ activeData.total_dependencies ?? 0 }} <small>{{ t('个外部依赖') }}</small></div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>{{ t('分类') }}</th><th>{{ t('依赖') }}</th><th>{{ t('文件数') }}</th><th>{{ t('文件') }}</th></tr></thead><tbody>
            <tr v-for="row in dependencyRows" :key="row.key">
              <td>{{ row.category }}</td>
              <td><b>{{ row.label }}</b></td>
              <td>{{ row.file_count }}</td>
              <td><code v-for="path in row.files || []" :key="path" class="block">{{ path }}</code></td>
            </tr>
          </tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'authorization_model'">
          <div class="metrics">
            <div class="metric">{{ activeData.protected_endpoints ?? 0 }} <small>{{ t('个受保护端点') }}</small></div>
            <div class="metric">{{ activeData.middleware_count ?? 0 }} <small>{{ t('个中间件') }}</small></div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>{{ t('函数') }}</th><th>{{ t('文件') }}</th><th>{{ t('认证级别') }}</th><th>{{ t('角色') }}</th><th>{{ t('权限') }}</th></tr></thead><tbody>
            <tr v-for="(item, index) in activeData.entries || []" :key="index">
              <td><b>{{ item.function }}</b></td>
              <td><code>{{ item.file }}:{{ item.line }}</code></td>
              <td>{{ authLevelLabel(item.auth_level) }}</td>
              <td>{{ item.roles?.join(', ') || '-' }}</td>
              <td>{{ item.permissions?.join(', ') || '-' }}</td>
            </tr>
          </tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'heat_map'">
          <div class="metrics">
            <div class="metric">{{ activeData.total_functions ?? 0 }} <small>{{ t('个函数') }}</small></div>
            <div class="metric">{{ activeData.hot ?? 0 }} <small>{{ t('热点') }}</small></div>
            <div class="metric">{{ activeData.warm ?? 0 }} <small>{{ t('温点') }}</small></div>
            <div class="metric">{{ activeData.cold ?? 0 }} <small>{{ t('冷点') }}</small></div>
          </div>
          <div class="table-wrap"><table><thead><tr><th>{{ t('函数') }}</th><th>{{ t('文件') }}</th><th>{{ t('类型') }}</th><th>{{ t('分层') }}</th><th>{{ t('调用方') }}</th><th>{{ t('被调用') }}</th><th>{{ t('总度数') }}</th><th>{{ t('热度') }}</th></tr></thead><tbody>
            <tr v-for="row in heatRows" :key="row.key">
              <td><b>{{ row.name }}</b><br><small class="muted">{{ row.qualified_name }}</small></td>
              <td><code>{{ row.file_path }}</code></td>
              <td>{{ row.kind }}</td>
              <td>{{ row.layer || '-' }}</td>
              <td>{{ row.callers }}</td>
              <td>{{ row.callees }}</td>
              <td>{{ row.total_degree }}</td>
              <td>{{ heatLabel(row.heat) }}</td>
            </tr>
          </tbody></table></div>
        </template>

        <template v-else-if="activeSection === 'page_api_map'">
          <UiState v-if="!pageApiData.pages?.length" kind="empty" :title="t('该 Snapshot 未发现前端页面')" :message="t('页面接口映射依赖快照冻结源码中的前端文件。')" />
          <template v-else>
            <div class="metrics">
              <div class="metric">{{ pageApiData.page_count }} <small>{{ t('个页面有接口调用') }}</small></div>
              <div class="metric">{{ (pageApiData.pages_without_calls || []).length }} <small>{{ t('个页面尚未发现接口调用') }}</small></div>
              <div class="metric">{{ pageApiData.orphan_call_count }} <small>{{ t('个调用未能归属到任何页面') }}</small></div>
              <div class="metric">{{ pageApiData.unresolved_imports }} <small>{{ t('处未解析的前端导入') }}</small></div>
            </div>
            <h3>{{ t('页面与接口调用关系图') }}</h3>
            <TopologyGraph :graph="pageApiGraph" data-testid="page-api-graph" :aria-label="t('页面与接口调用关系图')" :empty-text="t('当前 Snapshot 没有可展示的页面接口连线')" />
            <h3>{{ t('页面接口清单') }}</h3>
            <div class="table-wrap"><table data-testid="page-api-table"><thead><tr>
              <th>{{ t('页面') }}</th><th>{{ t('路由') }}</th><th>{{ t('方法') }}</th><th>{{ t('路径') }}</th><th>{{ t('前端函数') }}</th><th>{{ t('调用位置') }}</th><th>{{ t('匹配后端端点') }}</th>
            </tr></thead><tbody>
              <tr v-for="row in pageApiRows" :key="row.key">
                <td><b>{{ row.page }}</b></td>
                <td><code>{{ row.route || '-' }}</code></td>
                <td><span class="method">{{ row.method }}</span></td>
                <td><code>{{ row.path }}</code></td>
                <td>{{ row.function }}</td>
                <td><code>{{ row.call_site_file }}:{{ row.call_site_line }}</code></td>
                <td>{{ row.endpoint_matched ? t('是') : t('否') }}</td>
              </tr>
            </tbody></table></div>
            <details v-if="(pageApiData.unmapped_calls || []).length" class="page-api-details">
              <summary>{{ t('{count} 个调用未能归属到任何页面', { count: pageApiData.unmapped_calls.length }) }}</summary>
              <div class="table-wrap"><table><thead><tr>
                <th>{{ t('方法') }}</th><th>{{ t('路径') }}</th><th>{{ t('前端函数') }}</th><th>{{ t('定义位置') }}</th>
              </tr></thead><tbody>
                <tr v-for="(item, index) in pageApiData.unmapped_calls" :key="index">
                  <td><span class="method">{{ item.method }}</span></td>
                  <td><code>{{ item.path }}</code></td>
                  <td>{{ item.function }}</td>
                  <td><code>{{ item.definition_file }}</code></td>
                </tr>
              </tbody></table></div>
            </details>
          </template>
        </template>
        <template v-else-if="activeSection !== 'terms'">
          <div v-if="isDisabled(activeData)" class="empty-semantic">
            <p>{{ activeData.note }}</p><button class="primary" @click="loadLlm">{{ t('现在抽取') }}</button>
          </div>
          <pre v-else class="json-view">{{ formatJson(activeData) }}</pre>
        </template>
      </section>
    </div>

    <div v-else-if="snapshotId && !loading && !error" class="empty-state">{{ t('暂无知识数据') }}</div>

    <div v-if="endpointPromptEditor" class="endpoint-prompt-backdrop" @click.self="closeEndpointPrompt">
      <section
        class="endpoint-prompt-dialog"
        data-testid="endpoint-prompt-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="endpoint-prompt-title"
        tabindex="-1"
        @keydown.esc="closeEndpointPrompt"
      >
        <header class="endpoint-prompt-header">
          <div>
            <h2 id="endpoint-prompt-title">{{ t('自定义提示词') }}</h2>
            <p><span class="method">{{ endpointPromptEditor.item.method }}</span> <code>{{ endpointPromptEditor.item.path }}</code> · {{ endpointPromptEditor.item.handler }}</p>
          </div>
          <button class="endpoint-prompt-close" type="button" :aria-label="t('关闭自定义提示词')" @click="closeEndpointPrompt">×</button>
        </header>
        <div class="endpoint-prompt-body">
          <p class="prompt-modal-hint">{{ t('系统默认提示词会继续保留证据和 JSON 约束；下面的内容作为当前端点的补充指导。') }}</p>
          <details class="endpoint-prompt-default" open>
            <summary>{{ t('查看默认提示词') }}</summary>
            <pre>{{ endpointPromptDefault }}</pre>
          </details>
          <label class="prompt-field endpoint-prompt-field">
            <span>{{ t('基于默认提示词调整') }}</span>
            <textarea v-model="endpointPromptEditor.value" data-testid="endpoint-prompt-textarea" rows="12" maxlength="5000"></textarea>
          </label>
          <p class="prompt-hint">{{ t('仅本次端点生成使用，不会修改 Snapshot 级提示词或历史解释快照。') }}</p>
          <p v-if="endpointPromptEditor.error" class="explanation-error">{{ endpointPromptEditor.error }}</p>
        </div>
        <footer class="endpoint-prompt-actions">
          <button class="secondary" type="button" @click="closeEndpointPrompt">{{ t('取消') }}</button>
          <button class="primary" type="button" data-testid="endpoint-prompt-apply" :disabled="endpointPromptEditor.saving" @click="applyEndpointPrompt">
            {{ endpointPromptEditor.saving ? t('生成中...') : t('应用并生成') }}
          </button>
        </footer>
      </section>
    </div>

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
            <h2 id="sequence-zoom-title">{{ t('API 时序图') }}</h2>
            <p><span class="method">{{ sequenceZoom.method }}</span> <code>{{ sequenceZoom.path }}</code></p>
          </div>
          <button class="sequence-zoom-close" type="button" :aria-label="t('关闭放大时序图')" @click="closeSequenceZoom">×</button>
        </header>
        <div class="sequence-zoom-canvas">
          <div class="sequence-zoom-controls" :aria-label="t('时序图缩放控制')">
            <button type="button" :aria-label="t('缩小时序图')" :disabled="sequenceZoomScale <= 0.5" @click="changeSequenceZoom(-0.25)">−</button>
            <span aria-live="polite">{{ Math.round(sequenceZoomScale * 100) }}%</span>
            <button type="button" :aria-label="t('放大时序图')" :disabled="sequenceZoomScale >= 3" @click="changeSequenceZoom(0.25)">+</button>
            <button type="button" @click="resetSequenceZoom">{{ t('恢复原始大小') }}</button>
          </div>
          <div class="sequence-zoom-stage" :style="{ width: `${sequenceZoomScale * 100}%` }">
            <pre ref="sequenceZoomMermaid" class="mermaid">{{ sequenceZoom.mermaid }}</pre>
          </div>
        </div>
      </section>
    </div>
    </template>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'
import CallChainTree from '../components/CallChainTree.vue'
import LayerRules from '../components/LayerRules.vue'
import TopologyGraph from '../components/TopologyGraph.vue'
import { locale, t } from '../i18n.js'

function snapshotIdFromRoute(route) {
  return route?.query?.snapshot_id
    || new URLSearchParams(window.location.search).get('snapshot_id')
    || ''
}

const SECTIONS = [
  ['api_contract', 'API 契约', '阶段1', '路由、方法、处理函数与参数'],
  ['adr', 'ADR 决策记录', '阶段1', '架构决策记录（MADR 4.0）与 R1/R2 结构校验'],
  ['interface_contract', '公开接口', '阶段1', '导出的公共函数与类的公开方法——外部可访问面'],
  ['page_api_map', '页面接口映射', '阶段1', '路由页面与触发的 HTTP API 端点'],
  ['terms', '术语识别', '阶段1', '从 API 契约、实体定义和前端页面生成按可信度排序的业务术语'],
  ['module_topology', '模块拓扑', '阶段1', '业务域与实体划分、依赖关系与耦合度'],
  ['core_entities', '核心实体', '阶段1', '按字段、类型关系与领域语义识别核心领域对象'],
  ['layer_violations', '分层违规', '阶段1', '跨层依赖和架构边界违规'],
  ['config_consumption', '配置消费', '阶段2', '配置键与代码消费者的对应关系'],
  ['external_dependencies', '外部依赖', '阶段2', '外部服务、库与中间件分类'],
  ['authorization_model', '权限模型', '阶段2', '受保护端点、角色和权限'],
  ['heat_map', '代码热力图', '阶段2', '按调用关系识别热点和冷点函数'],
  ['business_descriptions', '业务描述', '阶段3', '核心函数的业务语义摘要', true],
  ['business_rules', '业务规则', '阶段3', '验证、转换、授权与工作流规则', true],
  ['error_catalog', '错误目录', '阶段3', '错误类型、触发条件与处理策略', true],
  ['state_machines', '状态机', '阶段3', '状态、转换和触发器', true],
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

const DEFAULT_ENDPOINT_GUIDANCE = '请完整分析该 API 的业务目的、输入输出、前置条件、业务流程、业务规则、状态变化、副作用、异常处理和外部依赖；所有结论必须有源码或调用链证据，无法确认的内容请明确标记。'
const DEFAULT_PROMPT_TEMPLATES = Object.freeze({
  local: '你是代码行为逆向分析器，为后续 Agent 设计方案提供可靠、可追溯的代码事实。\n\n请基于提供的源码范围分析函数职责、输入输出、前置条件、业务规则、状态变化、副作用和异常路径。\n\n用户补充分析要求：\n{guidance}\n\n只能依据源码分析，不得臆测；重要事实必须附源码行号；请输出合法 JSON，不要输出 Markdown。\n\n输出结构：\n{agent_output_schema}',
  synthesis: '你是代码事实合并器。请按源码顺序合并同一个函数的代码块分析结果，保留执行顺序、分支、异常、状态变化、副作用和源码行号，不得新增未经证实的推断。\n\n代码块解释：\n{chunk_explanations}\n\n用户补充分析要求：\n{guidance}\n\n请输出合法 JSON，不要输出 Markdown。\n\n输出结构：\n{agent_output_schema}',
  aggregate: '你是调用链业务行为聚合器。请先说明当前函数自身行为，再结合直接子节点解释和调用关系，描述调用顺序、条件、返回值使用、异常传播、副作用和 API 契约影响；区分当前节点与子节点行为，不得臆测。\n\n当前函数：{qualified_name}\n当前函数自身解释：\n{local_explanation}\n直接子节点解释：\n{children_explanations}\n调用关系：\n{call_edges}\n\n用户补充分析要求：\n{guidance}\n\n请输出合法 JSON，不要输出 Markdown。\n\n输出结构：\n{agent_output_schema}',
})

const ADR_COMMIT_STAGES = Object.freeze([
  'pending', 'queued', 'generating', 'generated', 'backtested',
  'approved', 'rejected', 'exported', 'skipped', 'failed',
])

const ADR_COMMIT_STAGE_LABELS = Object.freeze({
  pending: '待生成', queued: '排队中', generating: '生成中', generated: '待校验',
  backtested: '待审批', approved: '已通过', rejected: '已驳回', exported: '已导出',
  skipped: '已跳过', failed: '失败',
})

export default {
  components: { UiState, CallChainTree, LayerRules, TopologyGraph },
  props: { repoName: String },
  data() {
    // With createWebHashHistory the route query is part of the hash
    // (/#/repo/snapshot?snapshot_id=...), so window.location.search is empty.
    // Prefer Vue Router's parsed query and keep the window query as a fallback
    // for embedded/direct usages of this page.
    const snapshotId = snapshotIdFromRoute(this.$route)
    return {
      snapshotId,
      report: null, terms: [], termsLoading: false, activeSection: 'api_contract', llmLoaded: false,
      llmJob: null, llmPollTimer: null,
      taxonomyJob: null, taxonomyPollTimer: null,
      expandedModules: new Set(), moduleEvidence: {}, evidenceLoading: {},
      projectCatalog: [], catalogLoading: false, catalogError: null,
      expandedKeys: new Set(), expandedEntityKeys: new Set(),
      endpointSearch: '', endpointMethod: '', endpointService: '', endpointPage: 1, endpointPageSize: 25,
      loadedAt: '', loadDuration: 0,
      sequenceZoom: null,
      sequenceZoomScale: 1,
      apiExplanations: {},
      explanationPollTimers: {},
      explanationStatuses: {},
      explanationStatusSummary: { explained: 0, stale: 0, pending: 0, running: 0 },
      explanationStatusLoading: false,
      promptText: DEFAULT_ENDPOINT_GUIDANCE, promptDefaults: { ...DEFAULT_PROMPT_TEMPLATES }, promptTemplates: { ...DEFAULT_PROMPT_TEMPLATES }, promptCurrent: null, promptProfiles: [], promptSaving: false, promptError: '',
      batchJob: null, batchPollTimer: null, exporting: false,
      endpointCustomPrompts: {}, endpointPromptEditor: null,
      adrSourceKind: 'commit', adrCommitRef: '', adrForce: false,
      adrFilePath: '', adrPickedFile: null, adrText: '',
      adrSince: '', adrUntil: '', adrLimit: 10, adrIncludeMerges: false,
      adrSubmitting: false, adrError: '', adrGeneration: null, adrDraft: null, adrPollTimer: null,
      adrSuccessNote: '', adrSuccessDraftId: '',
      adrBatchId: '', adrBatch: null, adrBatchPollTimer: null, adrBatchNote: '', adrBatchSubmitting: false,
      adrCommits: [], adrCommitsLoading: false, adrCommitsError: '', adrCommitsScanned: 0, adrCommitsLoadedFor: '',
      adrCommitsTotal: 0, adrCommitsTotalAll: 0, adrCommitOffset: 0, adrCommitPageSize: 50,
      adrCommitCounts: {}, adrCommitProgress: null, adrStageFilter: '', adrCommitsSeq: 0,
      adrScanRunning: false, adrCommitFilterCandidates: true, adrCommitIncludeMerges: true, adrSelectedCommitShas: [], adrGeneratingSha: '',
      adrCandidateFilter: '', adrReasonFilter: '', adrCommitOrder: 'desc',
      adrPendingShas: [], adrPendingTotal: 0, adrPendingTruncated: false, adrCommitSelectionNote: '',
      adrOverridingSha: '',
      adrDrafts: [], adrDraftsLoading: false, adrDraftsError: '',
      adrExportingId: '', adrDeletingId: '', adrExportState: { id: '', path: '', error: '' },
      adrDetail: null, adrDetailLoading: false, adrDetailError: '', adrBacktestingId: '', adrBacktestError: '',
      adrReviewRating: null, adrReviewComment: '', adrReviewReviewer: '', adrReviewing: false, adrReviewError: '', adrReviewNotice: '',
      adrEditing: false, adrEditMarkdown: '', adrEditTitle: '', adrEditEditor: '', adrEditNote: '',
      adrEditSaving: false, adrEditError: '', adrRevisionPreview: null,
      adrPrompts: null, adrPromptKind: 'commit', adrPromptText: '', adrPromptSaving: false, adrPromptError: '', adrPromptNotice: '',
      adrTemplate: null, adrTemplateText: '', adrTemplateSaving: false, adrTemplateError: '', adrTemplateNotice: '',
    }
  },
  computed: {
    sections() {
      return SECTIONS.map(item => ({ ...item, label: this.t(item.label), phase: this.t(item.phase), description: this.t(item.description) }))
    },
    activeMeta() { return this.sections.find(item => item.key === this.activeSection) || this.sections[0] },
    activeData() { return this.activeSection === 'terms' ? this.terms : (this.report?.[this.activeSection] ?? {}) },
    pageApiData() { return this.report?.page_api_map || {} },
    pageApiRows() {
      const rows = []
      for (const page of this.pageApiData.pages || []) {
        for (const endpoint of page.endpoints || []) {
          rows.push({ key: `${page.name}|${endpoint.method}|${endpoint.path}|${endpoint.function}|${endpoint.call_site_file}`, page: page.name, route: page.route, ...endpoint })
        }
      }
      return rows
    },
    pageApiGraph() {
      const pages = (this.pageApiData.pages || []).filter(page => page.endpoints?.length)
      const endpointKey = endpoint => `${endpoint.method} ${String(endpoint.path || '').replace(/\{[^}]+\}|:[\w]+/g, '{}')}`
      const endpointMap = new Map()
      for (const page of pages) {
        for (const endpoint of page.endpoints || []) {
          const key = endpointKey(endpoint)
          if (!endpointMap.has(key)) endpointMap.set(key, { key, method: endpoint.method, path: endpoint.path })
          endpointMap.get(key).pages = (endpointMap.get(key).pages || new Set()).add(page.name)
        }
      }
      const endpointList = [...endpointMap.values()]
      const endpointIndex = new Map(endpointList.map((endpoint, index) => [endpoint.key, index]))
      const nodes = [], edges = []
      const PAGE_TOP = 30, PAGE_STEP = 72, EP_TOP = 30, EP_STEP = 60
      pages.forEach((page, index) => {
        nodes.push({ id: `page:${page.name}`, x: 24, y: PAGE_TOP + index * PAGE_STEP, width: 200, height: 52, kind: 'page', label: page.name, meta: page.route || '' })
      })
      endpointList.forEach((endpoint, index) => {
        nodes.push({ id: `ep:${endpoint.key}`, x: 480, y: EP_TOP + index * EP_STEP, width: 240, height: 44, kind: 'resource', label: `${endpoint.method} ${endpoint.path}` })
      })
      pages.forEach((page, index) => {
        const pageKeys = new Set((page.endpoints || []).map(endpointKey))
        for (const endpoint of endpointList) {
          if (!pageKeys.has(endpoint.key)) continue
          edges.push({
            id: `edge:${page.name}|${endpoint.key}`,
            kind: 'resource',
            x1: 224, y1: PAGE_TOP + index * PAGE_STEP + 26,
            x2: 480, y2: EP_TOP + endpointIndex.get(endpoint.key) * EP_STEP + 22,
          })
        }
      })
      const height = Math.max(
        PAGE_TOP + (pages.length - 1) * PAGE_STEP + 52,
        EP_TOP + (endpointList.length - 1) * EP_STEP + 44,
      ) + 30
      return { nodes, edges, width: 760, height }
    },
    moduleTree() {
      const nodes = new Map()
      for (const item of this.activeData?.modules || []) nodes.set(item.id, { ...item, childIds: [] })
      for (const node of nodes.values()) {
        const parent = node.parent_id ? nodes.get(node.parent_id) : null
        if (parent && parent.id !== node.id) parent.childIds.push(node.id)
      }
      const sortIds = ids => ids.sort((left, right) => {
        const a = nodes.get(left) || {}
        const b = nodes.get(right) || {}
        return (b.total_file_count || 0) - (a.total_file_count || 0) || String(left).localeCompare(String(right))
      })
      for (const node of nodes.values()) sortIds(node.childIds)
      const root = nodes.get('.')
      const top = root
        ? root.childIds
        : [...nodes.values()].filter(node => !node.parent_id || !nodes.get(node.parent_id)).map(node => node.id)
      return { nodes, top: sortIds(top) }
    },
    moduleRows() {
      const { nodes, top } = this.moduleTree
      const rows = []
      const walk = (id, level) => {
        const node = nodes.get(id)
        if (!node) return
        const files = node.files || []
        const hasChildren = node.childIds.length > 0 || files.length > 0
        rows.push({ type: 'module', key: 'm:' + id, level, node, hasChildren })
        if (!this.expandedModules.has(id)) return
        if (node.childIds.length) {
          for (const child of node.childIds) walk(child, level + 1)
          return
        }
        const evidence = this.moduleEvidence[id]
        for (const path of files) {
          const symbols = evidence?.byFile?.[path]
          rows.push({
            type: 'file', key: 'f:' + id + ':' + path, level: level + 1,
            path, moduleId: id, symbolCount: symbols ? symbols.length : null,
          })
          for (const symbol of symbols || []) {
            rows.push({ type: 'symbol', key: 's:' + path + ':' + symbol.kind + ':' + symbol.name, level: level + 2, symbol, path })
          }
        }
      }
      for (const id of top) walk(id, 0)
      return rows
    },
    moduleStats() {
      const counts = {}
      for (const module of this.activeData?.modules || []) {
        const kind = module.kind || 'module'
        counts[kind] = (counts[kind] || 0) + 1
      }
      const stats = []
      if (counts.domain) stats.push({ key: 'domain', label: this.t('个业务域'), value: counts.domain })
      if (counts.entity) stats.push({ key: 'entity', label: this.t('个实体'), value: counts.entity })
      if (counts.shared) stats.push({ key: 'shared', label: this.t('个公共模块'), value: counts.shared })
      if (!stats.length) stats.push({ key: 'module', label: this.t('个模块'), value: this.activeData?.module_count || 0 })
      return stats
    },
    dependencyRows() {
      return (this.activeData?.by_category || []).flatMap(category =>
        (category.dependencies || []).map(dep => ({ key: `${category.category}:${dep.label}`, category: category.category, ...dep })))
    },
    heatRows() {
      return [...(this.activeData?.hot_functions || []), ...(this.activeData?.warm_functions || [])]
        .map((row, index) => ({ key: `${index}:${row.qualified_name}`, ...row }))
    },
    llmJobActive() { return ['pending', 'running'].includes(this.llmJob?.status) },
    topologySource() { return this.activeData?.source || 'paths' },
    topologySourceLabel() {
      return this.t(({ llm: 'LLM 业务分类', heuristic: '文件名业务域', paths: '目录骨架' })[this.topologySource] || this.topologySource)
    },
    taxonomyMeta() { return this.activeData?.taxonomy || null },
    taxonomyActive() { return ['pending', 'running'].includes(this.taxonomyJob?.status) },
    taxonomyProgress() { return this.taxonomyJob?.progress?.percent ?? 0 },
    taxonomyStage() {
      const stage = String(this.taxonomyJob?.progress?.stage || '')
      if (stage.startsWith('assignment')) return this.t('实体归属')
      return this.t(({ queued: '排队中', inventory: '读取文件清单', domains: '提出业务域', classifying: '分类中', completed: '已完成', failed: '失败' })[stage] || stage || '排队中')
    },
    batchActive() { return ['queued', 'running'].includes(this.batchJob?.status) },
    adrSourceTabs() {
      return [
        { key: 'commit', label: this.t('提交') },
        { key: 'file', label: this.t('文件') },
        { key: 'text', label: this.t('一句话') },
        { key: 'history', label: this.t('历史') },
      ]
    },
    adrPromptTabs() {
      return [
        { key: 'commit', label: this.t('提交生成') },
        { key: 'file', label: this.t('文件生成') },
        { key: 'text', label: this.t('一句话生成') },
        { key: 'triage', label: this.t('提交分诊') },
        { key: 'backtest', label: this.t('回测') },
      ]
    },
    adrActiveGeneration() { return ['queued', 'running'].includes(this.adrGeneration?.status) },
    adrBatchActive() {
      return (this.adrBatch?.generations || []).some(item => ['queued', 'running'].includes(item.status))
    },
    adrBatchCounts() {
      const counts = this.adrBatch?.counts || {}
      if (Object.keys(counts).length) {
        return {
          queued: Number(counts.queued || 0),
          running: Number(counts.running || 0),
          succeeded: Number(counts.succeeded || 0),
          skipped: Number(counts.skipped || 0),
          failed: Number(counts.failed || 0),
        }
      }
      const result = { queued: 0, running: 0, succeeded: 0, skipped: 0, failed: 0 }
      for (const item of this.adrBatch?.generations || []) {
        if (result[item.status] !== undefined) result[item.status] += 1
      }
      return result
    },
    adrBatchTotal() {
      const counts = this.adrBatchCounts
      return counts.queued + counts.running + counts.succeeded + counts.skipped + counts.failed
    },
    adrBatchPercent() {
      return this.adrBatchProgress.percent
    },
    adrBatchProgress() {
      const server = this.adrBatch?.progress
      if (server && Number(server.total)) {
        return {
          total: Number(server.total),
          completed: Number(server.completed || 0),
          finished: Number(server.finished ?? ((server.completed || 0) + (server.skipped || 0) + (server.failed || 0))),
          failed: Number(server.failed || 0),
          skipped: Number(server.skipped || 0),
          running: Number(server.running || 0),
          queued: Number(server.queued || 0),
          percent: Number(server.percent || 0),
        }
      }
      const counts = this.adrBatchCounts
      const total = this.adrBatchTotal
      return {
        total,
        completed: counts.succeeded,
        finished: counts.succeeded + counts.skipped + counts.failed,
        failed: counts.failed,
        skipped: counts.skipped,
        running: counts.running,
        queued: counts.queued,
        percent: total
          ? Math.round(((counts.succeeded + counts.skipped + counts.failed) / total) * 100)
          : 0,
      }
    },
    adrPromptEntry() {
      return (this.adrPrompts?.kinds || []).find(item => item.source_kind === this.adrPromptKind) || null
    },
    adrPromptIsCustom() { return Boolean(this.adrPromptEntry?.prompt) },
    adrPromptVersionLabel() {
      const prompt = this.adrPromptEntry?.prompt
      return prompt ? this.t('自定义 v{version}', { version: prompt.version ?? 0 }) : this.t('系统默认')
    },
    adrPromptDigestValue() {
      const entry = this.adrPromptEntry
      return entry?.prompt?.prompt_digest || entry?.prompt_digest || ''
    },
    adrPromptPlaceholderHint() {
      return this.t('可用占位符：{{service}}、{{repo}}、{{source_ref}}、{{commit_message}}、{{commit_files}}、{{diff}}、{{content}}、{{text}}、{{schema}}、{{existing_adrs}}；未使用的占位符生成时为空。')
    },
    adrCommitCandidateCount() { return this.adrCandidateShas.length },
    adrCommitPage() { return Math.floor(this.adrCommitOffset / this.adrCommitPageSize) + 1 },
    adrCommitPageCount() {
      return Math.max(1, Math.ceil(Number(this.adrCommitsTotal || 0) / this.adrCommitPageSize))
    },
    adrCommitPageNumbers() {
      // Five page shortcuts around the current page, clamped to the first and
      // last page, so jumping several pages never needs repeated clicks.
      const total = this.adrCommitPageCount
      const current = this.adrCommitPage
      const size = Math.min(5, total)
      let start = Math.max(1, current - Math.floor(size / 2))
      const end = Math.min(total, start + size - 1)
      start = Math.max(1, end - size + 1)
      return Array.from({ length: end - start + 1 }, (_, index) => start + index)
    },
    adrCommitsPendingCount() {
      return Number(this.adrCommitCounts?.pending || 0)
    },
    adrCommitsProgress() {
      const progress = this.adrCommitProgress || {}
      const counts = this.adrCommitCounts || {}
      const total = Number(progress.total ?? this.adrCommitsTotalAll ?? 0)
      const processed = Number(progress.processed ?? (
        (counts.generated || 0) + (counts.backtested || 0) + (counts.approved || 0) +
        (counts.rejected || 0) + (counts.exported || 0) + (counts.skipped || 0) + (counts.failed || 0)
      ))
      const active = Number(progress.active ?? ((counts.queued || 0) + (counts.generating || 0)))
      const percent = progress.percent != null
        ? Number(progress.percent)
        : (total ? Math.round((processed * 100) / total) : 0)
      return {
        total: Number.isFinite(total) ? total : 0,
        processed: Number.isFinite(processed) ? processed : 0,
        active: Number.isFinite(active) ? active : 0,
        percent: Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0,
      }
    },
    adrStageOptions() {
      const counts = this.adrCommitCounts || {}
      return [
        { key: '', label: this.t('全部'), count: this.adrCommitsProgress.total },
        ...ADR_COMMIT_STAGES.map(stage => ({
          key: stage,
          label: this.adrCommitStageLabel(stage),
          count: Number(counts[stage] || 0),
        })),
      ]
    },
    adrCandidateShas() {
      return (this.adrCommits || []).filter(item => item.candidate).map(item => item.sha)
    },
    adrVisibleCommits() {
      const items = this.adrCommits || []
      // The server already filtered by stage; keep skipped rows visible in that view.
      if (this.adrStageFilter) return items
      // An explicit candidacy / skip-reason filter would contradict the
      // candidates-only view, so let the server-side filter win.
      if (this.adrCandidateFilter || this.adrReasonFilter) return items
      if (!this.adrCommitFilterCandidates || this.adrForce) return items
      return items.filter(item => item.candidate)
    },
    adrAllCandidatesSelected() {
      const shas = this.adrCandidateShas
      return shas.length > 0 && shas.every(sha => this.adrSelectedCommitShas.includes(sha))
    },
    adrOtherDrafts() {
      return (this.adrDrafts || []).filter(draft => (draft.source_kind || '') !== 'commit')
    },
    adrTemplateIsCustom() { return Boolean(this.adrTemplate?.template) },
    adrTemplateVersionLabel() {
      if (!this.adrTemplateIsCustom) return this.t('系统默认')
      const version = this.adrTemplate?.template_version ?? this.adrTemplate?.template?.version ?? 0
      return this.t('自定义 v{version}', { version })
    },
    adrTemplateDigest() { return this.adrTemplate?.template_digest || '' },
    adrTemplatePlaceholderHint() {
      return this.t('可用占位符：{{frontmatter}}、{{title}}、{{context}}、{{drivers}}、{{options}}、{{outcome}}、{{consequences}}、{{confirmation}}、{{options_detail}}、{{evidence}}、{{service}}、{{date}}、{{status}}；其中 {{title}} 为必填占位符，未使用的占位符渲染为空。')
    },
    llmProgress() {
      const progress = this.llmJob?.progress || {}
      return {
        percent: Number(progress.percent || 0),
        completed: Number(progress.completed || 0),
        total: Number(progress.total || 4),
      }
    },
    summaryCards() {
      return [
        { key: 'api_contract', label: this.t('API 端点'), value: this.report.api_contract?.endpoint_count ?? 0 },
        { key: 'interface_contract', label: this.t('公开接口'), value: this.report.interface_contract?.entry_count ?? 0 },
        { key: 'terms', label: this.t('术语'), value: this.terms.length },
        { key: 'module_topology', label: this.t('模块'), value: this.report.module_topology?.module_count ?? 0 },
        { key: 'core_entities', label: this.t('核心实体'), value: this.report.core_entities?.length ?? 0 },
        { key: 'layer_violations', label: this.t('分层违规'), value: this.report.layer_violations?.violation_count ?? 0 },
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
    promptReady() { return Boolean(this.promptText.trim() || Object.values(this.promptTemplates).some(value => String(value || '').trim())) },
  },
  watch: {
    activeSection(value) {
      this.$nextTick(this.revealActiveSection)
      if (value === 'adr') this.openAdrWorkbench()
    },
    adrForce(value) {
      if (value) return
      const candidates = new Set(this.adrCommits.filter(item => item.candidate).map(item => item.sha))
      this.adrSelectedCommitShas = this.adrSelectedCommitShas.filter(sha => candidates.has(sha))
    },
    adrStageFilter() {
      this.adrSelectedCommitShas = []
    },
    '$route': {
      async handler(route) {
        const snapshotId = snapshotIdFromRoute(route)
        if (snapshotId === this.snapshotId) return
        this.snapshotId = snapshotId
        this.report = null
        this.terms = []
        this.llmLoaded = false
        this.llmJob = null
        this.clearLlmPoll()
        this.promptText = DEFAULT_ENDPOINT_GUIDANCE; this.promptDefaults = { ...DEFAULT_PROMPT_TEMPLATES }; this.promptTemplates = { ...DEFAULT_PROMPT_TEMPLATES }; this.promptCurrent = null; this.promptProfiles = []; this.promptError = ''
        this.batchJob = null; this.clearBatchPoll()
        this.clearAdrPoll(); this.clearAdrBatchPoll()
        this.adrGeneration = null; this.adrDraft = null; this.adrSubmitting = false; this.adrError = ''
        this.adrSuccessNote = ''; this.adrSuccessDraftId = ''
        this.adrBatchId = ''; this.adrBatch = null; this.adrBatchNote = ''; this.adrBatchSubmitting = false
        this.adrCommits = []; this.adrCommitsError = ''; this.adrCommitsScanned = 0; this.adrCommitsLoadedFor = ''
        this.adrCommitsTotal = 0; this.adrCommitsTotalAll = 0; this.adrCommitOffset = 0
        this.adrPendingShas = []; this.adrPendingTotal = 0; this.adrPendingTruncated = false; this.adrCommitSelectionNote = ''
        this.adrCandidateFilter = ''; this.adrReasonFilter = ''; this.adrCommitOrder = 'desc'
        this.adrCommitCounts = {}; this.adrCommitProgress = null; this.adrStageFilter = ''; this.adrCommitsSeq += 1
        this.adrScanRunning = false; this.adrSelectedCommitShas = []; this.adrGeneratingSha = ''
        this.adrDrafts = []; this.adrDraftsError = ''; this.adrDetail = null; this.adrDetailError = ''
        this.adrBacktestingId = ''; this.adrBacktestError = ''
        this.adrReviewRating = null; this.adrReviewComment = ''; this.adrReviewReviewer = ''; this.adrReviewing = false; this.adrReviewError = ''; this.adrReviewNotice = ''
        this.adrEditing = false; this.adrEditMarkdown = ''; this.adrEditTitle = ''; this.adrEditEditor = ''; this.adrEditNote = ''
        this.adrEditSaving = false; this.adrEditError = ''; this.adrRevisionPreview = null
        this.adrExportingId = ''; this.adrDeletingId = ''; this.adrExportState = { id: '', path: '', error: '' }
        this.adrPrompts = null; this.adrPromptText = ''; this.adrPromptError = ''; this.adrPromptNotice = ''
        this.adrTemplate = null; this.adrTemplateText = ''; this.adrTemplateError = ''; this.adrTemplateNotice = ''
        this.adrPickedFile = null; this.adrFilePath = ''; this.adrCommitRef = ''; this.adrText = ''
        this.endpointCustomPrompts = {}; this.endpointPromptEditor = null
        this.projectCatalog = []
        this.error = null
        if (snapshotId) {
          await this.load(false)
        } else {
          await this.loadCatalog()
        }
      },
      deep: true,
    },
  },
  async created() {
    if (this.snapshotId) {
      await this.load(false)
    } else {
      await this.loadCatalog()
    }
  },
  beforeUnmount() {
    this.clearLlmPoll()
    this.clearTaxonomyPoll()
    this.clearBatchPoll()
    this.clearAdrPoll()
    this.clearAdrBatchPoll()
    for (const timer of Object.values(this.explanationPollTimers)) clearTimeout(timer)
  },
  methods: {
    t,
    revealActiveSection() {
      const nav = this.$refs.sectionNav
      const active = nav?.querySelector('button.active')
      if (!nav || !active || nav.scrollWidth <= nav.clientWidth) return
      const navRect = nav.getBoundingClientRect()
      const activeRect = active.getBoundingClientRect()
      nav.scrollTo({
        left: nav.scrollLeft + activeRect.left - navRect.left - (nav.clientWidth - activeRect.width) / 2,
        behavior: 'smooth',
      })
    },
    termTypeLabel(type) {
      const labels = {
        ENTITY: '实体', BUSINESS_EVENT: '业务事件', BUSINESS_ACTION: '业务动作',
        BUSINESS_ATTRIBUTE: '业务属性', BUSINESS_ENUM: '业务枚举', COMMAND: '命令',
        API_RESOURCE: 'API 资源', DTO: '数据传输对象', TECHNICAL_COMPONENT: '技术组件', PAGE: '页面',
      }
      return labels[type] ? this.t(labels[type]) : type || '-'
    },
    termConfidenceBandLabel(band) {
      const labels = { high: '高', medium: '中', low: '低' }
      return labels[band] ? this.t(labels[band]) : band || '-'
    },
    termStatusLabel(status) {
      const labels = { accepted: '已接纳', needs_review: '待审核', rejected: '已排除' }
      return labels[status] ? this.t(labels[status]) : status || '-'
    },
    adrStatusCount(status) {
      return this.report?.adr?.by_status?.[status] ?? 0
    },
    adrStatusKey(status) {
      const value = String(status || '').toLowerCase()
      if (value.startsWith('superseded')) return 'superseded'
      return value || 'unknown'
    },
    adrStatusLabel(status) {
      const value = String(status || '')
      if (value.toLowerCase().startsWith('superseded by')) {
        return this.t('被 {id} 替代', { id: value.replace(/^superseded by /i, '') })
      }
      const labels = { accepted: '已接受', proposed: '提议中', rejected: '已拒绝', deprecated: '已废弃' }
      return labels[value] ? this.t(labels[value]) : value || '-'
    },
    adrSupersedeText(row) {
      const parts = []
      if (row.superseded_by) parts.push(this.t('被 ADR-{id} 替代', { id: String(row.superseded_by).padStart(4, '0') }))
      if (row.supersedes?.length) {
        parts.push(this.t('替代 {ids}', { ids: row.supersedes.map(id => `ADR-${String(id).padStart(4, '0')}`).join(', ') }))
      }
      return parts.length ? parts.join(' · ') : '-'
    },
    // ── ADR workbench: generation, drafts, prompts ──
    adrErrorMessage(err, fallbackKey) {
      return (err?.body && (err.body.detail || err.body.message)) || err?.message || this.t(fallbackKey)
    },
    adrSourceKindLabel(kind) {
      const labels = { commit: '提交', file: '文件', text: '一句话', history: '历史' }
      return labels[kind] ? this.t(labels[kind]) : (kind || '-')
    },
    adrChangeTypeText(changeType) {
      const labels = {
        bugfix: '缺陷修复', docs: '文档', test: '测试', lint: '规范检查', dependency: '依赖升级',
        chore: '杂项', refactor: '重构', feature: '功能', architecture: '架构', merge: '合并', unknown: '未知',
      }
      return labels[changeType] ? this.t(labels[changeType]) : (changeType || '-')
    },
    adrReviewStatusText(status) {
      const labels = { pending: '待审批', approved: '已通过', rejected: '已驳回' }
      return labels[status] ? this.t(labels[status]) : (status ? status : this.t('待审批'))
    },
    adrBacktestVerdictKey(verdict) {
      return ['consistent', 'partial', 'contradicted', 'unverifiable'].includes(verdict) ? verdict : 'unknown'
    },
    adrBacktestVerdictText(verdict) {
      const labels = { consistent: '一致', partial: '部分一致', contradicted: '与代码矛盾', unverifiable: '无法验证' }
      return labels[verdict] ? this.t(labels[verdict]) : (verdict || '-')
    },
    adrBacktestStatusText(status) {
      const labels = { ready: '', failed: '回测失败', queued: '排队中', running: '回测中' }
      if (!status) return ''
      return this.t(labels[status] || status)
    },
    adrBacktestConfidencePercent(backtest) {
      const value = Number(backtest?.confidence)
      if (!Number.isFinite(value)) return 0
      return Math.max(0, Math.min(100, Math.round(value * 100)))
    },
    adrBacktestSummary(backtest) {
      if (!backtest) return '-'
      if (backtest.verdict) {
        return `${this.adrBacktestVerdictText(backtest.verdict)} · ${this.adrBacktestConfidencePercent(backtest)}%`
      }
      return this.adrBacktestStatusText(backtest.status) || '-'
    },
    adrDraftExportable(draft) {
      return draft?.review_status === 'approved'
    },
    adrCommitEditable(commit) {
      return Boolean(commit?.draft_id) && !commit?.exported_path
    },
    adrCommitSelectable(commit) {
      if (commit?.is_merge) return false
      // Failed generations stay selectable so a model error can be retried.
      return Boolean(commit?.candidate || commit?.stage === 'failed' || this.adrForce)
    },
    adrCommitStageKey(stage) {
      return ADR_COMMIT_STAGES.includes(stage) ? stage : 'pending'
    },
    adrCommitStageLabel(stage) {
      return this.t(ADR_COMMIT_STAGE_LABELS[this.adrCommitStageKey(stage)])
    },
    adrCommitStageText(commit) {
      const stage = this.adrCommitStageKey(commit?.stage)
      const label = this.adrCommitStageLabel(stage)
      if (stage === 'generating') {
        const progress = Math.max(0, Math.min(100, Math.round(Number(commit?.generation_progress || 0))))
        return this.t('{stage} {percent}%', { stage: label, percent: progress })
      }
      return label
    },
    adrCommitStageTitle(commit) {
      if (this.adrCommitStageKey(commit?.stage) !== 'skipped') return ''
      return commit?.generation_skip_reason || commit?.skip_reason || ''
    },
    adrCommitCandidateText(commit) {
      if (!commit?.candidate) return this.t('跳过')
      return commit.scanned ? this.t('候选') : this.t('规则候选')
    },
    adrCommitCandidateTitle(commit) {
      if (!commit?.candidate || commit.scanned) return ''
      return this.t('仅规则过滤通过，尚未 AI 分诊')
    },
    adrCommitCanGenerate(commit) {
      const stage = this.adrCommitStageKey(commit?.stage)
      // A failed generation is retryable: model errors must not become a dead end.
      if (stage === 'pending' || stage === 'failed') return true
      return stage === 'skipped' && this.adrForce
    },
    adrCommitGenerateDisabled(commit) {
      return !this.adrCommitCanGenerate(commit) || !this.adrCommitSelectable(commit)
        || this.adrGeneratingSha === commit.sha || this.adrBatchActive || this.adrScanRunning
        || this.adrActiveGeneration || this.adrSubmitting
    },
    adrCommitGenerateLabel(commit) {
      if (this.adrGeneratingSha === commit.sha) return this.t('生成中...')
      const stage = this.adrCommitStageKey(commit?.stage)
      if (stage === 'queued' || stage === 'generating') return this.adrCommitStageText(commit)
      if (stage === 'failed') return this.t('重试')
      return this.t('生成')
    },
    adrCommitCanBacktest(commit) {
      return Boolean(commit?.draft_id) && ['generated', 'backtested'].includes(this.adrCommitStageKey(commit?.stage))
    },
    adrCommitBacktestDisabled(commit) {
      return !this.adrCommitCanBacktest(commit) || this.adrBacktestingId === commit.draft_id
    },
    adrCommitReviewDisabled(commit) {
      return this.adrCommitStageKey(commit?.stage) !== 'backtested' || !commit?.draft_id
    },
    adrCommitCanExport(commit) {
      return this.adrCommitStageKey(commit?.stage) === 'approved'
        && Boolean(commit?.draft_id)
        && this.adrDraftExportable({ review_status: commit.review_status })
    },
    adrCommitExportDisabled(commit) {
      return !this.adrCommitCanExport(commit) || this.adrExportingId === commit.draft_id
    },
    adrCommitExportTitle(commit) {
      if (this.adrCommitCanExport(commit)) return ''
      return this.adrCommitStageKey(commit?.stage) === 'approved'
        ? this.t('需人工审批通过后才能导出')
        : this.adrCommitStageText(commit)
    },
    adrDraftStatusKey(status) {
      const value = String(status || '').toLowerCase()
      return ['proposed', 'accepted', 'rejected', 'deprecated', 'superseded'].includes(value) ? value : 'unknown'
    },
    adrDraftStatusText(status) {
      const labels = { proposed: '提议中', accepted: '已接受', rejected: '已拒绝', deprecated: '已废弃', superseded: '已替代' }
      return labels[status] ? this.t(labels[status]) : (status || '-')
    },
    adrGenerationStatusText(status) {
      const labels = { queued: '排队中', running: '生成中', succeeded: '成功', skipped: '已跳过', failed: '失败' }
      return labels[status] ? this.t(labels[status]) : (status || '-')
    },
    adrStageText(stage) {
      const labels = {
        generating: '生成中', calling_model: '调用模型', parse_failed: '解析响应失败',
        not_a_decision: '判定为非决策', draft_ready: '草稿已生成', skipped: '已跳过',
        failed: '失败', interrupted: '服务重启中断',
      }
      return this.t(labels[stage] || stage || '排队中')
    },
    shortAdrRef(ref) {
      const value = String(ref || '')
      return value.length > 24 ? `${value.slice(0, 24)}…` : value
    },
    shortAdrCommitSha(sha) {
      const value = String(sha || '')
      return value.length > 8 ? value.slice(0, 8) : value
    },
    async openAdrWorkbench() {
      if (!this.snapshotId) return
      const tasks = []
      if (this.adrCommitsLoadedFor !== this.snapshotId) tasks.push(this.loadAdrCommits())
      if (!this.adrTemplate) tasks.push(this.loadAdrTemplate())
      if (tasks.length) await Promise.all(tasks)
    },
    applyAdrCommits(data) {
      this.adrCommits = data?.items || []
      this.adrCommitsScanned = Number(data?.scanned || 0)
      this.adrCommitsTotal = Number(data?.total ?? this.adrCommits.length)
      this.adrCommitsTotalAll = Number(data?.total_all ?? this.adrCommitsTotal)
      this.adrPendingShas = Array.isArray(data?.pending_shas) ? data.pending_shas : []
      this.adrPendingTotal = Number(data?.pending_total ?? this.adrPendingShas.length)
      this.adrPendingTruncated = Boolean(data?.pending_truncated)
      if (data?.offset != null) this.adrCommitOffset = Number(data.offset)
      this.adrCommitCounts = data?.counts || {}
      this.adrCommitProgress = data?.progress || null
      this.adrCommitsLoadedFor = this.snapshotId
      const selectable = new Set(
        this.adrCommits.filter(item => item.candidate || this.adrForce).map(item => item.sha),
      )
      this.adrSelectedCommitShas = this.adrSelectedCommitShas.filter(sha => selectable.has(sha))
    },
    toggleAdrCommitOrder() {
      this.adrCommitOrder = this.adrCommitOrder === 'desc' ? 'asc' : 'desc'
      return this.resetAdrCommitPaging()
    },
    selectAllPendingCommits() {
      const shas = Array.isArray(this.adrPendingShas) ? this.adrPendingShas : []
      if (!shas.length) {
        this.adrCommitSelectionNote = this.t('当前没有待生成的候选提交。')
        return
      }
      this.adrSelectedCommitShas = [...shas]
      this.adrCommitSelectionNote = this.adrPendingTruncated
        ? this.t('待生成提交共 {total} 个，单批上限 {limit} 个，已按最新提交选取前 {limit} 个。', { total: this.adrPendingTotal, limit: shas.length })
        : this.t('已选中全部待生成提交（{count} 个）。', { count: shas.length })
    },
    goAdrCommitPage(page) {
      const next = Math.max(1, Math.min(Number(page) || 1, this.adrCommitPageCount))
      if (next === this.adrCommitPage) return undefined
      this.adrCommitOffset = (next - 1) * this.adrCommitPageSize
      this.adrCommitSelectionNote = ''
      this.adrSelectedCommitShas = []
      return this.loadAdrCommits()
    },
    resetAdrCommitPaging() {
      this.adrCommitOffset = 0
      this.adrCommitSelectionNote = ''
      this.adrSelectedCommitShas = []
      return this.loadAdrCommits()
    },
    setAdrCommitPageSize(value) {
      const size = Number(value) || 50
      this.adrCommitPageSize = Math.max(1, Math.min(size, 100))
      return this.resetAdrCommitPaging()
    },
    async filterAdrCommitsByStage(stage) {
      const next = stage === this.adrStageFilter ? '' : stage
      if (next === this.adrStageFilter) return
      this.adrStageFilter = next
      this.adrCommitOffset = 0
      await this.loadAdrCommits()
    },
    async loadAdrCommits() {
      if (!this.snapshotId) return
      const seq = this.adrCommitsSeq + 1
      this.adrCommitsSeq = seq
      this.adrCommitsLoading = true
      this.adrCommitsError = ''
      try {
        const data = await this.$api.get('/api/adr/commits', {
          snapshot_id: this.snapshotId,
          offset: this.adrCommitOffset,
          limit: this.adrCommitPageSize,
          since: this.adrSince || undefined,
          until: this.adrUntil || undefined,
          stage: this.adrStageFilter || undefined,
          candidate: this.adrCandidateFilter || undefined,
          reason: this.adrReasonFilter || undefined,
          order: this.adrCommitOrder,
          include_merges: this.adrCommitIncludeMerges,
        })
        if (seq === this.adrCommitsSeq) this.applyAdrCommits(data)
      } catch (err) {
        if (seq === this.adrCommitsSeq) this.adrCommitsError = this.adrErrorMessage(err, '读取提交列表失败')
      } finally {
        if (seq === this.adrCommitsSeq) this.adrCommitsLoading = false
      }
    },
    async scanAdrCommits() {
      if (!this.snapshotId || this.adrScanRunning) return
      this.adrScanRunning = true
      this.adrCommitsError = ''
      try {
        const data = await this.$api.request('/api/adr/commits/scan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            snapshot_id: this.snapshotId,
            offset: this.adrCommitOffset,
            limit: this.adrCommitPageSize,
            since: this.adrSince || '',
            until: this.adrUntil || '',
            stage: this.adrStageFilter || '',
            candidate: this.adrCandidateFilter,
            reason: this.adrReasonFilter,
            order: this.adrCommitOrder,
            include_merges: this.adrCommitIncludeMerges,
            refresh: false,
          }),
        })
        this.applyAdrCommits(data)
      } catch (err) {
        this.adrCommitsError = this.adrErrorMessage(err, 'AI 分诊提交失败')
      } finally {
        this.adrScanRunning = false
      }
    },
    toggleAllAdrCandidates(event) {
      const checked = Boolean(event?.target?.checked)
      const candidateShas = this.adrCandidateShas
      if (checked) {
        this.adrSelectedCommitShas = [...new Set([...this.adrSelectedCommitShas, ...candidateShas])]
      } else {
        const candidates = new Set(candidateShas)
        this.adrSelectedCommitShas = this.adrSelectedCommitShas.filter(sha => !candidates.has(sha))
      }
    },
    adrCommitRejectable(commit) {
      if (!commit || commit.is_merge || commit.overridden) return false
      const stage = this.adrCommitStageKey(commit.stage)
      return stage === 'skipped' && !commit.draft_id
    },
    async overrideAdrCommit(commit, restore = false) {
      if (!commit?.sha || this.adrOverridingSha || this.adrBatchActive) return
      this.adrOverridingSha = commit.sha
      this.adrError = ''
      this.adrCommitSelectionNote = ''
      try {
        await this.$api.request(`/api/adr/commits/${commit.sha}/override`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            snapshot_id: this.snapshotId,
            action: restore ? 'skip' : 'candidate',
            reason: restore ? '' : this.t('人工驳回跳过'),
          }),
        })
        await this.loadAdrCommits()
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, restore ? '恢复跳过失败' : '驳回跳过失败')
      } finally {
        this.adrOverridingSha = ''
      }
    },
    async generateAdrCommit(commit) {
      if (!commit?.sha || !this.adrCommitSelectable(commit)) return
      if (!this.snapshotId || this.adrSubmitting || this.adrActiveGeneration || this.adrBatchActive) return
      this.adrError = ''
      this.adrSuccessNote = ''
      this.adrSuccessDraftId = ''
      this.adrGeneratingSha = commit.sha
      this.adrSubmitting = true
      try {
        const data = await this.$api.request('/api/adr/generations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId, source: { kind: 'commit', ref: commit.sha }, force: this.adrForce }),
        })
        this.adrGeneration = data?.generation || data || null
        this.adrDraft = data?.draft || null
        if (this.adrActiveGeneration) this.scheduleAdrPoll()
        else await this.afterAdrGeneration()
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, '创建 ADR 生成任务失败')
      } finally {
        this.adrSubmitting = false
        this.adrGeneratingSha = ''
      }
    },
    backtestAdrCommit(commit) {
      if (!commit?.draft_id || this.adrCommitBacktestDisabled(commit)) return
      return this.runAdrBacktestForDraft({ id: commit.draft_id, backtest: commit.backtest })
    },
    reviewAdrCommit(commit) {
      if (!commit?.draft_id || this.adrCommitReviewDisabled(commit)) return
      return this.openAdrDraftForReview({ id: commit.draft_id })
    },
    exportAdrCommit(commit) {
      if (!commit?.draft_id || this.adrCommitExportDisabled(commit)) return
      return this.exportAdrDraft({ id: commit.draft_id, review_status: commit.review_status })
    },
    viewAdrCommit(commit) {
      if (!commit?.draft_id) return
      return this.openAdrDraft({ id: commit.draft_id })
    },
    editAdrCommit(commit) {
      if (!this.adrCommitEditable(commit)) return
      return this.openAdrDraft({ id: commit.draft_id }, { edit: true })
    },
    deleteAdrCommit(commit) {
      if (!commit?.draft_id) return
      return this.deleteAdrDraft({ id: commit.draft_id, title: commit.draft_title })
    },
    async submitAdrBatchGeneration() {
      if (!this.snapshotId || this.adrBatchSubmitting || this.adrBatchActive) return
      const commits = this.adrSelectedCommitShas.filter(sha => {
        const commit = this.adrCommits.find(item => item.sha === sha)
        return Boolean(commit && this.adrCommitSelectable(commit))
      })
      if (!commits.length) return
      this.adrError = ''
      this.adrBatchNote = ''
      this.adrSuccessNote = ''
      this.adrSuccessDraftId = ''
      this.adrBatchId = ''
      this.adrBatch = null
      this.clearAdrPoll()
      this.clearAdrBatchPoll()
      this.adrGeneration = null
      this.adrDraft = null
      this.adrBatchSubmitting = true
      try {
        const data = await this.$api.request('/api/adr/generations/batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId, commits, force: this.adrForce }),
        })
        this.adrBatchId = data?.batch_id || ''
        this.adrBatch = data || null
        if (!(data?.generations || []).length) {
          this.adrBatchNote = this.t('没有可生成的提交。')
          return
        }
        if (this.adrBatchActive) {
          this.scheduleAdrBatchPoll()
        } else {
          this.adrBatchNote = this.t('该批次没有需要执行的提交。')
          await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
        }
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, '创建批量生成任务失败')
      } finally {
        this.adrBatchSubmitting = false
      }
    },
    async loadAdrTemplate() {
      if (!this.snapshotId) return
      try {
        const data = await this.$api.get('/api/adr/template', { snapshot_id: this.snapshotId })
        this.adrTemplate = data
        this.adrTemplateText = data?.template?.template_text || data?.default_template || ''
        this.adrTemplateError = ''
      } catch (err) {
        this.adrTemplateError = this.adrErrorMessage(err, '读取 ADR 输出模板失败')
      }
    },
    async saveAdrTemplate() {
      if (!this.snapshotId || this.adrTemplateSaving || !this.adrTemplateText.trim()) return
      this.adrTemplateSaving = true
      this.adrTemplateError = ''
      this.adrTemplateNotice = ''
      try {
        const data = await this.$api.request('/api/adr/template', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId, template_text: this.adrTemplateText }),
        })
        this.adrTemplate = data
        this.adrTemplateText = data?.template?.template_text || data?.default_template || this.adrTemplateText
        this.adrTemplateNotice = this.t('已保存输出模板 v{version}', { version: data?.template?.version ?? data?.template_version ?? '' })
      } catch (err) {
        this.adrTemplateError = this.adrErrorMessage(err, '保存 ADR 输出模板失败')
      } finally {
        this.adrTemplateSaving = false
      }
    },
    async resetAdrTemplate() {
      if (!this.snapshotId || this.adrTemplateSaving) return
      if (!window.confirm(this.t('恢复系统默认输出模板？自定义版本将被删除。'))) return
      this.adrTemplateSaving = true
      this.adrTemplateError = ''
      this.adrTemplateNotice = ''
      try {
        const data = await this.$api.request('/api/adr/template', {
          method: 'DELETE',
          query: { snapshot_id: this.snapshotId },
        })
        this.adrTemplate = data
        this.adrTemplateText = data?.template?.template_text || data?.default_template || ''
        this.adrTemplateNotice = this.t('已恢复系统默认输出模板')
      } catch (err) {
        this.adrTemplateError = this.adrErrorMessage(err, '恢复默认 ADR 输出模板失败')
      } finally {
        this.adrTemplateSaving = false
      }
    },
    async loadAdrPrompts() {
      if (!this.snapshotId) return
      try {
        const data = await this.$api.get('/api/adr/prompts', { snapshot_id: this.snapshotId })
        this.adrPrompts = data
        this.adrPromptError = ''
        if (!this.adrPromptText.trim()) this.adrPromptText = this.adrPromptTextFor(this.adrPromptKind, data.kinds)
      } catch (err) {
        this.adrPromptError = this.adrErrorMessage(err, '读取 ADR 提示词失败')
      }
    },
    adrPromptTextFor(kind, kinds = this.adrPrompts?.kinds) {
      const entry = (kinds || []).find(item => item.source_kind === kind)
      return entry?.prompt?.prompt_text || entry?.default_prompt || ''
    },
    selectAdrPromptKind(kind) {
      this.adrPromptKind = kind
      this.adrPromptText = this.adrPromptTextFor(kind)
      this.adrPromptError = ''
      this.adrPromptNotice = ''
    },
    async loadAdrDrafts() {
      if (!this.snapshotId) return
      this.adrDraftsLoading = true
      this.adrDraftsError = ''
      try {
        const data = await this.$api.get('/api/adr/drafts', { snapshot_id: this.snapshotId })
        this.adrDrafts = [...(data?.drafts || [])]
      } catch (err) {
        this.adrDraftsError = this.adrErrorMessage(err, '读取 ADR 草稿失败')
      } finally {
        this.adrDraftsLoading = false
      }
    },
    async onAdrFilePicked(event) {
      const file = event?.target?.files?.[0]
      if (!file) {
        this.adrPickedFile = null
        return
      }
      try {
        const text = await file.text()
        this.adrPickedFile = { name: file.name, content: text }
        this.adrError = ''
      } catch (err) {
        this.adrPickedFile = null
        this.adrError = this.t('读取本地文件失败：{message}', { message: err?.message || '' })
      }
    },
    clearAdrPickedFile() {
      this.adrPickedFile = null
      const input = this.$el?.querySelector('[data-testid="adr-file-picker"]')
      if (input) input.value = ''
    },
    buildAdrSource() {
      const kind = this.adrSourceKind
      if (kind === 'commit') return { kind: 'commit', ref: (this.adrCommitRef || 'HEAD').trim() }
      if (kind === 'file') {
        if (this.adrPickedFile) {
          return { kind: 'file', filename: this.adrPickedFile.name, content: this.adrPickedFile.content }
        }
        if (this.adrFilePath.trim()) return { kind: 'file', path: this.adrFilePath.trim() }
        this.adrError = this.t('请填写仓库内文件路径，或选择一个本地文件。')
        return null
      }
      if (kind === 'text') {
        const text = this.adrText.trim()
        if (!text) {
          this.adrError = this.t('请填写一句话决策描述。')
          return null
        }
        return { kind: 'text', text }
      }
      return null
    },
    async submitAdrGeneration() {
      if (!this.snapshotId || this.adrSubmitting || this.adrActiveGeneration) return
      this.adrError = ''
      this.adrSuccessNote = ''
      this.adrSuccessDraftId = ''
      const source = this.buildAdrSource()
      if (!source) return
      this.adrSubmitting = true
      try {
        const data = await this.$api.request('/api/adr/generations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId, source, force: this.adrForce }),
        })
        this.adrGeneration = data?.generation || data || null
        this.adrDraft = data?.draft || null
        if (this.adrActiveGeneration) this.scheduleAdrPoll()
        else await this.afterAdrGeneration()
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, '创建 ADR 生成任务失败')
      } finally {
        this.adrSubmitting = false
      }
    },
    async submitAdrHistory() {
      if (!this.snapshotId || this.adrSubmitting || this.adrBatchActive) return
      this.adrError = ''
      this.adrBatchNote = ''
      this.adrSuccessNote = ''
      this.adrSuccessDraftId = ''
      this.adrBatchId = ''
      this.adrBatch = null
      this.clearAdrPoll()
      this.adrGeneration = null
      this.adrDraft = null
      this.adrSubmitting = true
      try {
        const limit = Number(this.adrLimit) > 0 ? Math.min(Number(this.adrLimit), 50) : 10
        const data = await this.$api.request('/api/adr/history', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            snapshot_id: this.snapshotId,
            since: this.adrSince || '',
            until: this.adrUntil || '',
            limit,
            include_merges: this.adrIncludeMerges,
            force: this.adrForce,
          }),
        })
        this.adrBatchId = data?.batch_id || ''
        this.adrBatch = data || null
        if (!(data?.generations || []).length) {
          this.adrBatchNote = this.t('没有匹配到可生成的提交。')
          return
        }
        if (this.adrBatchActive) {
          this.scheduleAdrBatchPoll()
        } else {
          this.adrBatchNote = this.t('该批次没有需要执行的提交。')
          await this.loadAdrDrafts()
        }
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, '创建历史批量生成任务失败')
      } finally {
        this.adrSubmitting = false
      }
    },
    async afterAdrGeneration() {
      const generation = this.adrGeneration
      if (!generation) return
      if (generation.status === 'succeeded') {
        this.adrSuccessNote = this.t('草稿已生成：{title}', { title: this.adrDraft?.title || generation.draft_id || '' })
        this.adrSuccessDraftId = this.adrDraft?.id || generation.draft_id || ''
      }
      if (['succeeded', 'skipped', 'failed'].includes(generation.status)) {
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      }
    },
    scheduleAdrPoll() {
      this.clearAdrPoll()
      this.adrPollTimer = setTimeout(() => this.refreshAdrGeneration(), 1200)
    },
    clearAdrPoll() {
      if (this.adrPollTimer) clearTimeout(this.adrPollTimer)
      this.adrPollTimer = null
    },
    async refreshAdrGeneration() {
      this.adrPollTimer = null
      if (!this.snapshotId || !this.adrGeneration?.id) return
      try {
        const data = await this.$api.get(`/api/adr/generations/${encodeURIComponent(this.adrGeneration.id)}`)
        this.adrGeneration = data?.generation || this.adrGeneration
        this.adrDraft = data?.draft || null
        if (this.adrActiveGeneration) {
          this.scheduleAdrPoll()
          // Keep the commit table lifecycle status in sync while a generation runs.
          await this.loadAdrCommits()
        } else {
          await this.afterAdrGeneration()
        }
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, '读取 ADR 生成状态失败')
        if (this.adrActiveGeneration) this.scheduleAdrPoll()
      }
    },
    scheduleAdrBatchPoll() {
      this.clearAdrBatchPoll()
      this.adrBatchPollTimer = setTimeout(() => this.refreshAdrBatch(), 1500)
    },
    clearAdrBatchPoll() {
      if (this.adrBatchPollTimer) clearTimeout(this.adrBatchPollTimer)
      this.adrBatchPollTimer = null
    },
    async refreshAdrBatch() {
      this.adrBatchPollTimer = null
      if (!this.snapshotId || !this.adrBatchId) return
      const wasActive = this.adrBatchActive
      try {
        const data = await this.$api.get(`/api/adr/batches/${encodeURIComponent(this.adrBatchId)}`)
        this.adrBatch = data
      } catch (err) {
        this.adrError = this.adrErrorMessage(err, '读取批量生成进度失败')
      }
      if (this.adrBatchActive) {
        this.scheduleAdrBatchPoll()
        // Refresh the commit table alongside the batch poll so stages advance live.
        await this.loadAdrCommits()
      } else if (wasActive) {
        this.adrBatchNote = this.t('批量生成结束：成功 {succeeded}，跳过 {skipped}，失败 {failed}。', this.adrBatchCounts)
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      }
    },
    async openAdrDraft(draft, options = {}) {
      if (!draft?.id) return
      this.adrDetailLoading = true
      this.adrDetailError = ''
      this.adrBacktestError = ''
      try {
        const data = await this.$api.get(`/api/adr/drafts/${encodeURIComponent(draft.id)}`)
        const detailDraft = data?.draft || null
        this.adrDetail = {
          draft: detailDraft,
          generation: data?.generation || null,
          backtest: data?.backtest || null,
          revisions: data?.revisions || [],
        }
        this.adrReviewRating = detailDraft?.rating ?? null
        this.adrReviewComment = detailDraft?.review_comment || ''
        this.adrReviewReviewer = detailDraft?.reviewer || ''
        this.adrReviewError = ''
        this.adrReviewNotice = ''
        this.adrEditing = false
        this.adrEditError = ''
        this.adrRevisionPreview = null
        if (options?.edit && detailDraft && !detailDraft.exported_path) {
          this.startAdrEdit(detailDraft)
        }
      } catch (err) {
        this.adrDetailError = this.adrErrorMessage(err, '读取 ADR 草稿详情失败')
      } finally {
        this.adrDetailLoading = false
      }
    },
    openAdrDraftForReview(draft) {
      this.openAdrDraft(draft)
    },
    adrDraftEditable(draft) {
      return Boolean(draft?.id) && !draft?.exported_path
    },
    startAdrEdit(draft) {
      if (!this.adrDraftEditable(draft)) return
      this.adrEditing = true
      this.adrEditError = ''
      this.adrEditMarkdown = draft.markdown || ''
      this.adrEditTitle = draft.title || ''
      this.adrEditEditor = draft.editor || ''
      this.adrEditNote = ''
      this.adrRevisionPreview = null
    },
    cancelAdrEdit() {
      this.adrEditing = false
      this.adrEditError = ''
      this.adrEditMarkdown = ''
      this.adrEditTitle = ''
      this.adrEditEditor = ''
      this.adrEditNote = ''
    },
    previewAdrRevision(revision) {
      this.adrRevisionPreview = this.adrRevisionPreview?.id === revision?.id ? null : revision
    },
    async saveAdrEdit() {
      const draft = this.adrDetail?.draft
      if (!draft?.id || this.adrEditSaving) return
      if (!this.adrEditMarkdown.trim()) {
        this.adrEditError = this.t('ADR 内容不能为空')
        return
      }
      this.adrEditSaving = true
      this.adrEditError = ''
      try {
        const data = await this.$api.request(`/api/adr/drafts/${encodeURIComponent(draft.id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            markdown: this.adrEditMarkdown,
            title: this.adrEditTitle.trim(),
            editor: this.adrEditEditor.trim(),
            note: this.adrEditNote.trim(),
          }),
        })
        if (this.adrDetail?.draft?.id === draft.id) {
          this.adrDetail = {
            ...this.adrDetail,
            draft: data?.draft || this.adrDetail.draft,
            revisions: data?.revisions || [],
            backtest: data?.backtest ?? null,
          }
        }
        this.adrEditing = false
        this.adrReviewRating = null
        this.adrReviewComment = ''
        this.adrReviewNotice = this.t('已保存为版本 v{revision}，需重新审批后才能导出。', { revision: data?.draft?.revision ?? '' })
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      } catch (err) {
        this.adrEditError = this.adrErrorMessage(err, '保存 ADR 版本失败')
      } finally {
        this.adrEditSaving = false
      }
    },
    closeAdrDetail() {
      this.adrDetail = null
      this.adrDetailError = ''
      this.adrBacktestError = ''
      this.adrReviewError = ''
      this.adrReviewNotice = ''
    },
    async runAdrBacktest() {
      const draft = this.adrDetail?.draft
      if (!draft?.id || this.adrBacktestingId) return
      this.adrBacktestingId = draft.id
      this.adrBacktestError = ''
      try {
        const data = await this.$api.request(`/api/adr/drafts/${encodeURIComponent(draft.id)}/backtest`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force: Boolean(this.adrDetail?.backtest) }),
        })
        if (this.adrDetail?.draft?.id === draft.id) {
          this.adrDetail = { ...this.adrDetail, backtest: data?.backtest || null }
        }
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      } catch (err) {
        this.adrBacktestError = this.adrErrorMessage(err, '运行 ADR 回测失败')
      } finally {
        this.adrBacktestingId = ''
      }
    },
    async runAdrBacktestForDraft(draft) {
      if (!draft?.id || this.adrBacktestingId) return
      this.adrBacktestingId = draft.id
      this.adrBacktestError = ''
      try {
        const data = await this.$api.request(`/api/adr/drafts/${encodeURIComponent(draft.id)}/backtest`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force: Boolean(draft.backtest) }),
        })
        if (this.adrDetail?.draft?.id === draft.id) {
          this.adrDetail = { ...this.adrDetail, backtest: data?.backtest || null }
        }
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      } catch (err) {
        this.adrBacktestError = this.adrErrorMessage(err, '运行 ADR 回测失败')
      } finally {
        this.adrBacktestingId = ''
      }
    },
    async submitAdrReview(action) {
      const draft = this.adrDetail?.draft
      if (!draft?.id || this.adrReviewing) return
      this.adrReviewing = true
      this.adrReviewError = ''
      this.adrReviewNotice = ''
      try {
        const body = { action, comment: this.adrReviewComment.trim(), reviewer: this.adrReviewReviewer.trim() }
        if (this.adrReviewRating != null && this.adrReviewRating !== '') {
          body.rating = Number(this.adrReviewRating)
        }
        const data = await this.$api.request(`/api/adr/drafts/${encodeURIComponent(draft.id)}/review`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (this.adrDetail?.draft?.id === draft.id) {
          this.adrDetail = {
            ...this.adrDetail,
            draft: data?.draft || this.adrDetail.draft,
            backtest: data?.backtest ?? this.adrDetail.backtest,
          }
        }
        this.adrReviewNotice = action === 'approve'
          ? this.t('已通过审批，草稿可以导出。')
          : this.t('已驳回草稿。')
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      } catch (err) {
        this.adrReviewError = this.adrErrorMessage(err, '提交 ADR 审批失败')
      } finally {
        this.adrReviewing = false
      }
    },
    async exportAdrDraft(draft) {
      if (!draft?.id || this.adrExportingId) return
      this.adrExportingId = draft.id
      this.adrExportState = { id: draft.id, path: '', error: '' }
      try {
        const data = await this.$api.request(`/api/adr/drafts/${encodeURIComponent(draft.id)}/export`, { method: 'POST' })
        this.adrExportState = { id: draft.id, path: data?.relative_path || data?.path || '', error: '' }
        if (this.adrDetail?.draft?.id === draft.id && data?.draft) {
          this.adrDetail = { ...this.adrDetail, draft: data.draft }
        }
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      } catch (err) {
        this.adrExportState = { id: draft.id, path: '', error: this.adrErrorMessage(err, '导出 ADR 草稿失败') }
      } finally {
        this.adrExportingId = ''
      }
    },
    async deleteAdrDraft(draft) {
      if (!draft?.id || this.adrDeletingId) return
      if (!window.confirm(this.t('删除草稿「{title}」？已导出到仓库的文件不会被删除。', { title: draft.title || draft.id }))) return
      this.adrDeletingId = draft.id
      try {
        await this.$api.delete(`/api/adr/drafts/${encodeURIComponent(draft.id)}`)
        if (this.adrExportState.id === draft.id) this.adrExportState = { id: '', path: '', error: '' }
        if (this.adrDetail?.draft?.id === draft.id) this.closeAdrDetail()
        await Promise.all([this.loadAdrDrafts(), this.loadAdrCommits()])
      } catch (err) {
        this.adrDraftsError = this.adrErrorMessage(err, '删除 ADR 草稿失败')
      } finally {
        this.adrDeletingId = ''
      }
    },
    async saveAdrPrompt() {
      if (!this.snapshotId || this.adrPromptSaving || !this.adrPromptText.trim()) return
      this.adrPromptSaving = true
      this.adrPromptError = ''
      this.adrPromptNotice = ''
      try {
        const data = await this.$api.request('/api/adr/prompts', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId, source_kind: this.adrPromptKind, prompt_text: this.adrPromptText }),
        })
        const kinds = data?.kinds || this.adrPrompts?.kinds || []
        this.adrPrompts = { ...(this.adrPrompts || {}), kinds }
        this.adrPromptText = this.adrPromptTextFor(this.adrPromptKind, kinds)
        this.adrPromptNotice = this.t('已保存为自定义 v{version}', { version: data?.prompt?.version ?? '' })
      } catch (err) {
        this.adrPromptError = this.adrErrorMessage(err, '保存 ADR 提示词失败')
      } finally {
        this.adrPromptSaving = false
      }
    },
    async resetAdrPrompt() {
      if (!this.snapshotId || this.adrPromptSaving) return
      if (!window.confirm(this.t('恢复该来源的系统默认提示词？自定义版本将被删除。'))) return
      this.adrPromptSaving = true
      this.adrPromptError = ''
      this.adrPromptNotice = ''
      try {
        const data = await this.$api.request('/api/adr/prompts', {
          method: 'DELETE',
          query: { snapshot_id: this.snapshotId, source_kind: this.adrPromptKind },
        })
        const kinds = data?.kinds || this.adrPrompts?.kinds || []
        this.adrPrompts = { ...(this.adrPrompts || {}), kinds }
        this.adrPromptText = this.adrPromptTextFor(this.adrPromptKind, kinds)
        this.adrPromptNotice = this.t('已恢复系统默认提示词')
      } catch (err) {
        this.adrPromptError = this.adrErrorMessage(err, '恢复默认 ADR 提示词失败')
      } finally {
        this.adrPromptSaving = false
      }
    },
    async loadCatalog() {
      this.catalogLoading = true
      this.catalogError = null
      try {
        const data = await this.$api.get('/api/scopes')
        const projects = []
        for (const scope of data.scopes || []) {
          const members = await this.$api.get(`/api/scopes/${encodeURIComponent(scope.id)}/members`)
          for (const member of members.members || []) {
            const snapshots = await this.$api.get(`/api/repository-members/${encodeURIComponent(member.id)}/snapshots`, { limit: 1 })
            const snapshot = snapshots.items?.[0]
            if (snapshot?.id) {
              projects.push({
                scope_id: scope.id,
                scope_name: scope.name,
                member_id: member.id,
                display_name: member.display_name,
                snapshot_id: snapshot.id,
              })
            }
          }
        }
        this.projectCatalog = projects
      } catch (error) {
        this.catalogError = error
      } finally {
        this.catalogLoading = false
      }
    },
    knowledgeLink(project) {
      return {
        name: 'knowledge',
        params: { repoName: project.display_name || project.member_id },
        query: { snapshot_id: project.snapshot_id },
      }
    },
    async load(includeLlm) {
      const started = performance.now()
      if (!this.snapshotId) {
        this.error = null
        return
      }
      await this.$runAsync(async () => {
        this.report = await this.$api.get('/api/knowledge', { snapshot_id: this.snapshotId, include_llm: includeLlm, complete: true })
        await Promise.all([this.loadPromptProfiles(), this.loadBatchStatus(), this.loadTerms(), this.loadExplanationStatuses(), this.loadTaxonomyState(), this.loadAdrPrompts(), this.loadAdrDrafts()])
        if (this.activeSection === 'adr') await Promise.all([this.loadAdrCommits(), this.loadAdrTemplate()])
        for (const timer of Object.values(this.explanationPollTimers)) clearTimeout(timer)
        this.explanationPollTimers = {}
        this.apiExplanations = {}
        this.llmJob = this.report?.llm_job || null
        this.llmLoaded = Boolean(this.report?.llm_job?.status === 'completed' || (includeLlm && !this.report?.llm_job))
        if (this.llmJobActive) this.scheduleLlmPoll()
        else this.clearLlmPoll()
        this.endpointPage = 1
        this.endpointService = ''
        this.expandedKeys = new Set()
        this.expandedEntityKeys = new Set()
        this.expandedModules = new Set()
        this.moduleEvidence = {}
        this.evidenceLoading = {}
        this.loadedAt = new Date().toLocaleTimeString()
        this.loadDuration = Math.round(performance.now() - started)
      })
    },
    async loadTerms() {
      if (!this.snapshotId) return
      this.termsLoading = true
      try {
        const data = await this.$api.get('/api/terms', { snapshot_id: this.snapshotId, limit: 500 })
        this.terms = data.terms || []
      } catch {
        this.terms = []
      } finally {
        this.termsLoading = false
      }
    },
    async loadPromptProfiles() {
      if (!this.snapshotId) return
      try {
        const data = await this.$api.get('/api/api-explanation-prompts', { repository_snapshot_id: this.snapshotId })
        this.promptCurrent = data?.current || null
        this.promptProfiles = data?.profiles || []
        this.promptText = this.promptCurrent?.prompt_text || data?.default_guidance || DEFAULT_ENDPOINT_GUIDANCE
        this.promptDefaults = this.mergePromptTemplates(DEFAULT_PROMPT_TEMPLATES, data?.defaults)
        this.promptTemplates = this.mergePromptTemplates(this.promptDefaults, this.promptCurrent?.prompt_templates)
      } catch (err) {
        this.promptDefaults = { ...DEFAULT_PROMPT_TEMPLATES }
        this.promptTemplates = { ...DEFAULT_PROMPT_TEMPLATES }
        this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('读取提示词失败')
      }
    },
    mergePromptTemplates(defaults, overrides) {
      const result = { ...DEFAULT_PROMPT_TEMPLATES }
      for (const source of [defaults, overrides]) {
        for (const key of ['local', 'synthesis', 'aggregate']) {
          const value = source?.[key]
          if (typeof value === 'string' && value.trim()) result[key] = value
        }
      }
      return result
    },
    async savePrompt() {
      if (!this.snapshotId || !this.promptReady) return
      this.promptSaving = true; this.promptError = ''
      try {
        const data = await this.$api.request('/api/api-explanation-prompts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repository_snapshot_id: this.snapshotId, prompt_text: this.promptText, templates: this.promptTemplates }) })
        this.promptCurrent = data.profile; this.promptProfiles = [data.profile, ...this.promptProfiles.filter(item => item.id !== data.profile.id)]
        this.promptTemplates = this.mergePromptTemplates(this.promptDefaults, data.profile?.prompt_templates)
      } catch (err) { this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('保存提示词失败') }
      finally { this.promptSaving = false }
    },
    async saveAndApplyBatch() {
      if (!this.snapshotId || !this.promptReady || this.batchActive) return
      const count = this.report?.api_contract?.endpoint_count || 0
      if (!window.confirm(this.t('将为 {count} 个 API 端点生成解释，可能产生模型调用费用。是否继续？', { count }))) return
      this.promptSaving = true; this.promptError = ''
      try {
        const prompt = await this.$api.request('/api/api-explanation-prompts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repository_snapshot_id: this.snapshotId, prompt_text: this.promptText, templates: this.promptTemplates }) })
        this.promptCurrent = prompt.profile; this.promptProfiles = [prompt.profile, ...this.promptProfiles.filter(item => item.id !== prompt.profile.id)]
        this.promptTemplates = this.mergePromptTemplates(this.promptDefaults, prompt.profile?.prompt_templates)
        const response = await this.$api.request('/api/api-explanations/batches', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repository_snapshot_id: this.snapshotId, prompt_profile_id: prompt.profile.id, mode: 'all', concurrency: 2 }) })
        this.batchJob = response?.batch || response
        if (this.batchActive) this.scheduleBatchPoll()
      } catch (err) { this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('创建批量任务失败') }
      finally { this.promptSaving = false }
    },
    async refreshStaleBatch() {
      if (!this.snapshotId || this.batchActive) return
      const count = this.explanationStatusSummary.stale || 0
      if (!count || !window.confirm(this.t('将为 {count} 个过期端点重新生成解释，是否继续？', { count }))) return
      this.promptSaving = true; this.promptError = ''
      try {
        const response = await this.$api.request('/api/api-explanations/batches', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repository_snapshot_id: this.snapshotId, prompt_profile_id: this.promptCurrent?.id || '', mode: 'stale', concurrency: 2 }) })
        this.batchJob = response?.batch || response
        if (this.batchActive) this.scheduleBatchPoll()
      } catch (err) { this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('创建批量任务失败') }
      finally { this.promptSaving = false }
    },
    async exportApiExplanations() {
      if (!this.snapshotId || this.exporting) return
      this.exporting = true; this.promptError = ''
      try {
        const data = await this.$api.get('/api/api-explanations/export', { repository_snapshot_id: this.snapshotId, format: 'markdown' })
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url; link.download = `api-explanations-${this.snapshotId}.json`
        link.click(); URL.revokeObjectURL(url)
      } catch (err) { this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('导出 API 契约失败') }
      finally { this.exporting = false }
    },
    async loadBatchStatus() {
      if (!this.snapshotId) return
      try {
        const data = await this.$api.get('/api/api-explanations/batches', { repository_snapshot_id: this.snapshotId })
        const batches = data?.batches || []
        this.batchJob = batches.find(item => ['queued', 'running'].includes(item.status)) || batches[0] || null
        if (this.batchActive) this.scheduleBatchPoll(); else this.clearBatchPoll()
      } catch (err) { /* Batch history is optional for older servers. */ }
    },
    async loadExplanationStatuses() {
      if (!this.snapshotId) return
      this.explanationStatuses = {}
      this.explanationStatusSummary = { explained: 0, stale: 0, pending: 0, running: 0 }
      this.explanationStatusLoading = true
      try {
        const data = await this.$api.get('/api/api-explanations/statuses', { repository_snapshot_id: this.snapshotId })
        const map = {}
        for (const item of data?.endpoints || []) if (item?.api_key) map[item.api_key] = item
        this.explanationStatuses = map
        this.explanationStatusSummary = data?.summary || { explained: 0, stale: 0, pending: 0, running: 0 }
      } catch (err) {
        /* Status projection is optional; keep the table usable if it fails. */
      } finally { this.explanationStatusLoading = false }
    },
    scheduleBatchPoll() { this.clearBatchPoll(); this.batchPollTimer = setTimeout(() => this.refreshBatchStatus(), 1200) },
    clearBatchPoll() { if (this.batchPollTimer) clearTimeout(this.batchPollTimer); this.batchPollTimer = null },
    async refreshBatchStatus() {
      this.batchPollTimer = null
      if (!this.batchJob?.id) return
      const wasActive = this.batchActive
      try { const data = await this.$api.get(`/api/api-explanations/batches/${encodeURIComponent(this.batchJob.id)}`); this.batchJob = data?.batch || data } catch (err) { /* retry below */ }
      if (this.batchActive) this.scheduleBatchPoll()
      else if (wasActive) await this.loadExplanationStatuses()
    },
    async cancelBatch() {
      if (!this.batchJob?.id || !window.confirm(this.t('确定取消当前批量任务吗？'))) return
      try { const data = await this.$api.request(`/api/api-explanations/batches/${encodeURIComponent(this.batchJob.id)}/cancel`, { method: 'POST' }); this.batchJob = data?.batch || data; if (this.batchActive) this.scheduleBatchPoll() } catch (err) { this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('取消任务失败') }
    },
    async retryBatch() {
      if (!this.batchJob?.id) return
      try { const data = await this.$api.request(`/api/api-explanations/batches/${encodeURIComponent(this.batchJob.id)}/retry-failed`, { method: 'POST' }); this.batchJob = data?.batch || data; this.scheduleBatchPoll() } catch (err) { this.promptError = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('重试失败端点失败') }
    },
    promptVersion(profileId) { const profile = this.promptProfiles.find(item => item.id === profileId); return profile ? `v${profile.version}` : profileId },
    restorePromptTemplate(key) { this.promptTemplates = { ...this.promptTemplates, [key]: this.promptDefaults[key] || '' } },
    restoreAllPromptTemplates() { this.promptTemplates = { ...this.promptDefaults } },
    batchStatusText(status) { return this.t(({ queued: '排队中', running: '生成中', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', skipped: '已跳过' })[status] || status || '') },
    async loadLlm() {
      if (!window.confirm(this.t('LLM 知识抽取可能需要较长时间并产生 API 调用费用，是否继续？'))) return
      if (!this.snapshotId || this.llmJobActive) return
      try {
        const response = await this.$api.request('/api/knowledge', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId }),
        })
        this.llmJob = response?.job || response || null
        this.llmLoaded = this.llmJob?.status === 'completed'
        await this.load(true)
        if (this.llmJobActive) this.scheduleLlmPoll()
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('抽取 LLM 知识失败')
        this.error = { message: detail }
      }
    },
    llmStageText(stage) {
      return this.t(({ starting: '准备中', queued: '排队中', business_descriptions: '业务描述', business_rules: '业务规则', error_catalog: '错误目录', state_machines: '状态机', completed: '已完成', failed: '失败' })[stage] || stage || '排队中')
    },
    clearLlmPoll() {
      if (this.llmPollTimer) clearTimeout(this.llmPollTimer)
      this.llmPollTimer = null
    },
    scheduleLlmPoll() {
      this.clearLlmPoll()
      this.llmPollTimer = setTimeout(() => this.refreshLlmStatus(), 1200)
    },
    async refreshLlmStatus() {
      this.llmPollTimer = null
      if (!this.snapshotId) return
      try {
        const report = await this.$api.get('/api/knowledge', { snapshot_id: this.snapshotId, include_llm: false, complete: true })
        this.report = report
        this.llmJob = report?.llm_job || null
        this.llmLoaded = this.llmJob?.status === 'completed'
        if (this.llmJobActive) this.scheduleLlmPoll()
      } catch (err) {
        if (this.llmJobActive) this.scheduleLlmPoll()
      }
    },
    isDisabled(value) { return Boolean(value && !Array.isArray(value) && value.note) },
    authLevelLabel(level) {
      const labels = { authenticated: '已认证', public: '公开', middleware: '中间件', denied: '拒绝', unknown: '未知' }
      return labels[level] ? this.t(labels[level]) : level || '-'
    },
    heatLabel(heat) {
      const labels = { hot: '热点', warm: '温点', cold: '冷点' }
      return labels[heat] ? this.t(labels[heat]) : heat || '-'
    },
    moduleKindLabel(kind) {
      const labels = { domain: '业务域', entity: '实体', shared: '公共', root: '根模块' }
      return labels[kind] ? this.t(labels[kind]) : ''
    },
    moduleLayers(item) {
      return Object.entries(item.layers || {}).map(([name, count]) => ({ name, count }))
    },
    moduleTitle(item) {
      const zh = item.label || ''
      const en = item.label_en || ''
      const label = locale.value === 'en' ? (en || zh) : (zh || en)
      return label || item.segment || item.name
    },
    symbolKindLabel(kind) {
      return this.t(({ class: '类', function: '函数', component: '组件', method: '方法' })[kind] || kind || '')
    },
    async toggleModule(node) {
      if (!node) return
      const hasChildren = (node.childIds?.length || 0) > 0 || (node.files?.length || 0) > 0
      if (!hasChildren) return
      if (this.expandedModules.has(node.id)) {
        this.expandedModules.delete(node.id)
        return
      }
      this.expandedModules.add(node.id)
      if (!node.childIds?.length && !this.moduleEvidence[node.id]) {
        await this.loadModuleEvidence(node.id)
      }
    },
    async loadModuleEvidence(moduleId) {
      if (!this.snapshotId) return
      this.evidenceLoading = { ...this.evidenceLoading, [moduleId]: true }
      try {
        const data = await this.$api.get('/api/module-evidence', { snapshot_id: this.snapshotId, module_id: moduleId })
        const byFile = {}
        for (const file of data?.files || []) byFile[file.path] = file.symbols || []
        this.moduleEvidence = { ...this.moduleEvidence, [moduleId]: { files: data?.files || [], byFile } }
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('加载模块证据失败')
        this.error = { message: detail }
      } finally {
        const next = { ...this.evidenceLoading }
        delete next[moduleId]
        this.evidenceLoading = next
      }
    },
    async loadTaxonomyState() {
      if (!this.snapshotId) return
      try {
        const data = await this.$api.get('/api/business-taxonomy', { snapshot_id: this.snapshotId })
        this.taxonomyJob = data?.job || null
        if (this.taxonomyActive) this.scheduleTaxonomyPoll()
      } catch (err) {
        this.taxonomyJob = null
      }
    },
    async classifyBusiness() {
      if (!window.confirm(this.t('LLM 业务分类会调用模型并产生费用，是否继续？'))) return
      if (!this.snapshotId || this.taxonomyActive) return
      try {
        const response = await this.$api.request('/api/business-taxonomy', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ snapshot_id: this.snapshotId }),
        })
        this.taxonomyJob = response?.job || response || null
        this.scheduleTaxonomyPoll()
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('LLM 业务分类失败')
        this.error = { message: detail }
      }
    },
    async resetTaxonomy() {
      if (!window.confirm(this.t('恢复目录骨架并删除已保存的 LLM 分类？'))) return
      if (!this.snapshotId) return
      try {
        await this.$api.delete(`/api/business-taxonomy?snapshot_id=${encodeURIComponent(this.snapshotId)}`)
        this.taxonomyJob = null
        await this.load(false)
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('恢复目录分类失败')
        this.error = { message: detail }
      }
    },
    clearTaxonomyPoll() {
      if (this.taxonomyPollTimer) clearTimeout(this.taxonomyPollTimer)
      this.taxonomyPollTimer = null
    },
    scheduleTaxonomyPoll() {
      this.clearTaxonomyPoll()
      this.taxonomyPollTimer = setTimeout(() => this.refreshTaxonomyStatus(), 1200)
    },
    async refreshTaxonomyStatus() {
      this.taxonomyPollTimer = null
      if (!this.snapshotId) return
      try {
        const data = await this.$api.get('/api/business-taxonomy', { snapshot_id: this.snapshotId })
        this.taxonomyJob = data?.job || null
        if (this.taxonomyActive) this.scheduleTaxonomyPoll()
        else if (this.taxonomyJob?.status === 'completed') await this.load(false)
      } catch (err) {
        if (this.taxonomyActive) this.scheduleTaxonomyPoll()
      }
    },
    formatJson(value) { return JSON.stringify(value, null, 2) },
    scoreBreakdownText(breakdown) {
      return Object.entries(breakdown || {})
        .filter(([key]) => key !== 'total')
        .map(([key, value]) => `${key} ${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(2)}`)
        .join(' · ')
    },
    endpointKey(item, index) { return `${item.method || ''}-${item.path || ''}-${index}` },
    explanationKey(item) { return [this.repoName || '', item.repository || '', item.method || '', item.path || '', item.handler || ''].join('||') },
    apiKey(item) { return item.api_key || [String(item.method || '').toUpperCase(), item.path || '', item.handler || ''].join('|') },
    endpointStatusFor(item) { return this.explanationStatuses[this.apiKey(item)]?.status || 'pending' },
    endpointStatusText(status) { return this.t(({ explained: '已解释', stale: '待刷新', pending: '待解释', running: '生成中' })[status] || status || '') },
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
    explanationQuery(item) { return { repository_snapshot_id: this.snapshotId, api_key: this.apiKey(item) } },
    unwrapCurrent(data) {
      if (!data || data.status === 'missing') return null
      const snapshot = data.snapshot || data.current || data.current_snapshot || (data.id ? data : null)
      return snapshot && data.freshness ? { ...snapshot, freshness: data.freshness } : snapshot
    },
    unwrapSnapshots(data) { return Array.isArray(data) ? data : (data?.snapshots || []) },
    async loadEndpointExplanation(item, { quiet = false } = {}) {
      if (!quiet) this.setExplanationState(item, { loading: true, error: '' })
      const wasRunning = this.explanationState(item).running
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
        else if (wasRunning) await this.loadExplanationStatuses()
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('读取解释失败')
        this.setExplanationState(item, { loading: false, error: detail })
        if (quiet && this.explanationState(item).running) this.scheduleExplanationPoll(item)
      }
    },
    endpointPromptStorageKey(item) { return `codeevolution:endpoint-prompt:${this.snapshotId}:${this.apiKey(item)}` },
    endpointStoredPrompt(item) {
      const key = this.endpointPromptStorageKey(item)
      if (this.endpointCustomPrompts[key] !== undefined) return this.endpointCustomPrompts[key]
      try { return window.localStorage?.getItem(key) || '' } catch { return '' }
    },
    endpointPromptDefault() {
      return this.promptDefaults.aggregate || DEFAULT_ENDPOINT_GUIDANCE
    },
    openEndpointPrompt(item) {
      const stored = this.endpointStoredPrompt(item)
      this.endpointPromptEditor = {
        item,
        value: stored || this.promptText.trim() || DEFAULT_ENDPOINT_GUIDANCE,
        saving: false,
        error: '',
      }
      this.$nextTick(() => this.$el.querySelector('[data-testid="endpoint-prompt-dialog"]')?.focus())
    },
    closeEndpointPrompt() {
      if (!this.endpointPromptEditor?.saving) this.endpointPromptEditor = null
    },
    async applyEndpointPrompt() {
      const editor = this.endpointPromptEditor
      if (!editor || !editor.value.trim()) return
      editor.saving = true
      editor.error = ''
      const item = editor.item
      const key = this.endpointPromptStorageKey(item)
      const prompt = editor.value.trim()
      this.endpointCustomPrompts = { ...this.endpointCustomPrompts, [key]: prompt }
      try { window.localStorage?.setItem(key, prompt) } catch { /* Storage may be unavailable in embedded browsers. */ }
      await this.generateEndpointExplanation(item, prompt)
      if (this.endpointPromptEditor === editor) this.endpointPromptEditor = null
    },
    async generateEndpointExplanation(item, customPrompt = null) {
      this.clearExplanationPoll(item)
      this.setExplanationState(item, { running: true, status: 'running', error: '' })
      try {
        const prompt = customPrompt === null ? this.endpointStoredPrompt(item) : customPrompt.trim()
        const payload = { repository_snapshot_id: this.snapshotId, repo: this.repoName || '', member: item.repository || '', method: item.method || '', path: item.path || '', handler: item.handler || '', file: item.file || '', line: Number(item.line || 0) }
        if (prompt) payload.custom_prompt = prompt
        const response = await this.$api.request('/api/api-explanations/generate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
        const snapshot = response.snapshot || response
        const snapshots = snapshot?.id ? [snapshot, ...this.explanationState(item).snapshots.filter(existing => existing.id !== snapshot.id)] : this.explanationState(item).snapshots
        this.setExplanationState(item, { snapshots, running: ['pending', 'running'].includes(snapshot?.status || 'running') })
        if (['completed', 'partial', 'failed'].includes(snapshot?.status)) await this.loadEndpointExplanation(item, { quiet: true })
        else this.scheduleExplanationPoll(item)
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('生成解释失败')
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
      const warning = isCurrent ? this.t('这是当前解释快照，删除后该 API 将进入无快照状态。是否删除？') : this.t('是否删除该解释快照？')
      if (!window.confirm(warning)) return
      this.setExplanationState(item, { deleting: snapshot.id, error: '' })
      try {
        const suffix = isCurrent ? '?confirm_current=true' : ''
        await this.$api.delete(`/api/api-explanations/snapshots/${encodeURIComponent(snapshot.id)}${suffix}`)
        await this.loadEndpointExplanation(item, { quiet: true })
        await this.loadExplanationStatuses()
      } catch (err) {
        const detail = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('删除快照失败')
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
    partialWarning(snapshot) {
      const coverage = this.snapshotCoverage(snapshot) || {}
      const issues = Array.isArray(coverage.issues) ? coverage.issues.length : 0
      const partial = Number(coverage.partial_nodes || 0)
      if (snapshot?.status !== 'partial' && !coverage.truncated && !issues && !partial) return ''
      const reasons = []
      if (coverage.truncated) reasons.push(this.t('调用图被截断'))
      if (partial) reasons.push(this.t('部分节点 {count} 个', { count: partial }))
      if (issues) reasons.push(this.t('覆盖问题 {count} 项', { count: issues }))
      return this.t('解释不完整：{reason}。结果仍会发布，请结合源码核对。', { reason: reasons.join(this.t('、')) || this.t('存在未完全覆盖的节点') })
    },
    snapshotNodes(snapshot) { return snapshot?.nodes || snapshot?.node_explanations || [] },
    nodeSummary(node) {
      const value = node.aggregate_explanation || node.local_explanation || node.explanation
      if (!value) return ''
      if (typeof value === 'object') return value.summary || value.business_purpose_zh || value.business_purpose_en || ''
      try { const parsed = JSON.parse(value); return parsed.summary || parsed.business_purpose_zh || parsed.business_purpose_en || '' } catch { return value }
    },
    explanationStatusText(status) { return this.t(({ pending: '等待中', running: '生成中', completed: '已完成', partial: '部分完成', failed: '失败', stale: '已过期', missing: '未生成' })[status] || status || '') },
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
    // ── business description & rules (derived from the API explanation snapshot) ──

    businessFacts(item) {
      const explanation = this.snapshotExplanation(this.explanationState(item).current)
      if (!explanation) return null
      const summary = explanation.summary || explanation.business_purpose_zh || explanation.business_purpose_en || ''
      const flow = (explanation.business_flow || explanation.main_flow || []).map(step => this.explanationFactText(step))
      const rules = (explanation.business_rules || []).map(rule => this.explanationFactText(rule))
      const effects = (explanation.side_effects || []).map(effect => this.explanationFactText(effect))
      if (!summary && !flow.length && !rules.length) return null
      return { summary, flow, rules, effects }
    },

    explanationFactText(value) {
      if (typeof value === 'string') return value
      const text = value?.rule || value?.action || value?.detail || value?.summary || value?.description || JSON.stringify(value)
      const lines = (Array.isArray(value?.evidence_lines) ? value.evidence_lines : []).filter(line => Number.isInteger(line))
      return lines.length ? `${text} · ${this.t('源码行 {lines}', { lines: lines.join(', ') })}` : text
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
.primary, .secondary { display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 6px; padding: 8px 13px; cursor: pointer; text-decoration: none; }
.primary { background: #e94560; color: white; }
.secondary { background: #ececf2; color: #333; }
.notice { background: #fff8e1; color: #795c12; border: 1px solid #ffe6a3; border-radius: 6px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px; }
.llm-progress { background: #eef7ff; color: #2a6496; border: 1px solid #cfe5f8; border-radius: 6px; padding: 11px 14px; font-size: 13px; margin-bottom: 16px; }
.llm-progress-heading { display: flex; justify-content: space-between; gap: 12px; }
.llm-progress-track { height: 7px; margin: 8px 0 6px; overflow: hidden; border-radius: 5px; background: #dbeaf6; }
.llm-progress-track span { display: block; height: 100%; border-radius: inherit; background: #388ac2; transition: width .25s ease; }
.llm-progress p, .llm-error { margin: 0; font-size: 11px; }
.llm-error { margin-bottom: 16px; padding: 10px 14px; border: 1px solid #f2c5ce; border-radius: 6px; background: #fff0f2; color: #b8324a; }
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
.kind-badge { display: inline-block; margin-left: 6px; padding: 1px 6px; border-radius: 8px; background: #eef1f6; color: #55637a; font-size: 9px; font-weight: 500; vertical-align: middle; }
.kind-badge.kind-domain { background: #fff0f2; color: #c82d48; }
.kind-badge.kind-entity { background: #eef7f0; color: #2f7a4d; }
.kind-badge.kind-shared { background: #f4f0fb; color: #6a4ea8; }
.layer-chips { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0; }
.layer-chip { padding: 1px 6px; border-radius: 8px; border: 1px solid #e5e8ee; color: #667085; font-size: 10px; }
.layer-chip small { color: #9aa3b2; }
.topology-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.source-badge { padding: 3px 9px; border-radius: 10px; font-size: 12px; background: #eef1f6; color: #55637a; }
.source-badge.source-llm { background: #eef7f0; color: #2f7a4d; }
.source-badge.source-heuristic { background: #fff4e5; color: #8a5a12; }
.llm-note { margin-bottom: 12px; padding: 8px 12px; border: 1px solid #dfe6ef; border-radius: 6px; background: #f7f9fc; color: #55637a; font-size: 12px; }
.module-description { color: #667085; font-size: 12px; margin: 2px 0 4px; }
.module-tree { display: flex; flex-direction: column; border: 1px solid #e5e8ee; border-radius: 8px; overflow: hidden; background: #fff; }
.tree-row { display: flex; align-items: center; gap: 8px; min-height: 32px; padding-right: 10px; font-size: 13px; border-bottom: 1px solid #f1f3f7; }
.tree-row:last-child { border-bottom: none; }
.tree-row:hover { background: #f7f9fc; }
.tree-level-0 { font-weight: 600; background: #fbfcfe; }
.tree-level-1 { font-weight: 500; }
.tree-toggle { width: 18px; height: 18px; flex: none; border: none; background: transparent; color: #667085; cursor: pointer; padding: 0; font-size: 11px; line-height: 1; }
.tree-toggle-empty { cursor: default; }
.tree-label { border: none; background: transparent; padding: 0; font: inherit; color: #1f2d3d; cursor: pointer; text-align: left; }
.tree-meta { color: #8b95a5; font-size: 11px; white-space: nowrap; }
.tree-description { color: #667085; font-size: 11px; }
.tree-file { font-size: 11px; color: #3c4a5c; background: transparent; }
.tree-file-mark { color: #b6bfcc; font-size: 9px; }
.tree-symbol { font-size: 12px; color: #3c4a5c; }
.symbol-kind { font-size: 9px; padding: 1px 5px; border-radius: 6px; background: #eef1f6; color: #55637a; }
.symbol-kind.sym-class { background: #eef7f0; color: #2f7a4d; }
.symbol-kind.sym-function { background: #eef4fd; color: #2f5fa8; }
.symbol-kind.sym-component { background: #fdf2e9; color: #97601f; }
.tree-empty { color: #8b95a5; font-size: 13px; padding: 12px; margin: 0; }
.layer-conflict { color: #b7791f; font-size: 10px; margin-top: 4px; }
.table-wrap { overflow-x: auto; }
.table-tools { display: flex; align-items: end; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.terms-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; padding: 12px; background: #f8f9fc; border-radius: 6px; color: #667085; font-size: 13px; }
.term-score { color: #1a8050; font-weight: 700; }
.term-status { padding: 3px 7px; border-radius: 10px; font-size: 11px; background: #f0f0f5; }
.term-status.accepted { background: #e8f7ed; color: #23764a; }
.term-status.needs_review { background: #fff3db; color: #8a6418; }
.term-status.rejected { background: #fcebed; color: #b8324a; }
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
.block { display: block; }
.repo-badge { display: inline-block; margin-right: 5px; padding: 1px 5px; border-radius: 8px; background: #fff0f2; color: #c82d48; font-size: 9px; }
.api-explanation-section { margin-top: 16px; border-top: 1px solid #e2e2e8; padding-top: 12px; }
.api-prompt-panel, .batch-card { margin: 0 0 16px; padding: 13px; border: 1px solid #e2e5eb; border-radius: 7px; background: #fafafd; }
.api-prompt-heading, .batch-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.api-prompt-heading h3, .batch-heading h3 { margin: 0 0 4px; font-size: 13px; color: #333; }
.api-prompt-heading p, .batch-heading p { margin: 0; color: #888; font-size: 11px; }
.api-prompt-heading > span { color: #2a6496; font-size: 11px; }
.prompt-default-heading, .prompt-field-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.prompt-default-heading { margin-top: 12px; color: #555; font-size: 11px; font-weight: 600; }
.prompt-default-hint { margin: 5px 0 6px; color: #777; font-size: 10px; line-height: 1.45; }
.prompt-default-reference { margin: 0 0 8px; padding: 7px 9px; border: 1px solid #e2e5eb; border-radius: 5px; background: #fff; font-size: 10px; }
.prompt-default-reference summary { cursor: pointer; color: #2a6496; }
.prompt-default-item { margin-top: 8px; }
.prompt-default-item b { display: block; margin-bottom: 3px; color: #555; }
.prompt-default-item pre { max-height: 220px; overflow: auto; margin: 0; padding: 7px; white-space: pre-wrap; color: #555; background: #f5f6f8; border-radius: 3px; font: 10px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.api-prompt-panel textarea { width: 100%; margin: 10px 0 8px; border: 1px solid #cfd3da; border-radius: 5px; padding: 8px; font: 12px/1.5 sans-serif; resize: vertical; }
.prompt-field { display: block; margin-top: 10px; color: #555; font-size: 11px; }
.prompt-field span { display: block; margin-bottom: 4px; font-weight: 600; }
.prompt-field-heading > span { margin-bottom: 0; }
.link-button { padding: 0; border: 0; color: #2a6496; background: transparent; cursor: pointer; font-size: 10px; }
.link-button:disabled { color: #aaa; cursor: not-allowed; }
.prompt-field textarea { margin: 0; min-height: 96px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.prompt-hint { margin: 6px 0 8px; color: #888; font-size: 10px; line-height: 1.45; }
.api-prompt-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.prompt-history, .batch-items { margin-top: 10px; font-size: 11px; }
.prompt-history summary, .batch-items summary { cursor: pointer; color: #555; }
.prompt-history-item, .batch-item { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-top: 1px solid #e8e8ed; }
.prompt-history-item:first-of-type, .batch-item:first-of-type { margin-top: 6px; }
.prompt-history-item span { flex: 1; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prompt-history-item small, .batch-item small { color: #999; }
.batch-heading > strong { color: #388ac2; font-size: 20px; }
.batch-counts { margin: 8px 0; color: #666; font-size: 11px; }
.batch-cancelling { color: #8a6500; font-size: 11px; }
.batch-cancel-hint { margin: 7px 0 0; color: #8a6500; font-size: 11px; }
.explanation-warning { margin: 8px 0 0; padding: 7px 9px; border-radius: 5px; background: #fff8e6; color: #8a6500; font-size: 11px; }
.batch-item code { min-width: 210px; }
.api-explanation-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.api-explanation-heading h4 { margin-bottom: 3px; font-size: 13px; color: #333; }
.api-explanation-actions { display: flex; gap: 6px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
.explanation-status { display: inline-block; margin-left: 5px; padding: 2px 6px; border-radius: 8px; background: #eeeef3; color: #666; font-size: 10px; font-weight: 400; }
.explanation-running, .explanation-pending { background: #e3f0fc; color: #2a6496; }
.explanation-completed { background: #eaf8f0; color: #23764a; }
.explanation-partial, .explanation-stale { background: #fff3d6; color: #8a6500; }
.explanation-explained { background: #eaf8f0; color: #23764a; }
.table-tools .explanation-status-summary { margin-left: 14px; color: #555; }
.table-tools .explanation-status-summary.loading { opacity: .55; }
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
.br-source { color: #8a6500; font-size: 10px; margin-left: 6px; font-weight: 400; }
.br-result { font-size: 12px; }
.br-parsed { display: grid; gap: 6px; }
.br-purpose { color: #333; line-height: 1.5; }
.br-detail { font-size: 11px; }
.br-detail summary { color: #555; cursor: pointer; padding: 3px 0; }
.br-detail ol, .br-detail ul { margin: 4px 0 4px 18px; }
.br-detail li { padding: 2px 0; color: #444; line-height: 1.4; }
.endpoint-prompt-backdrop { position: fixed; z-index: 110; inset: 0; display: grid; place-items: center; padding: 20px; background: rgba(17, 17, 28, .68); }
.endpoint-prompt-dialog { width: min(760px, 100%); max-height: min(860px, 92vh); display: flex; flex-direction: column; overflow: hidden; background: white; border-radius: 10px; box-shadow: 0 20px 70px rgba(0, 0, 0, .4); }
.endpoint-prompt-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 16px 18px; border-bottom: 1px solid #e5e5eb; }
.endpoint-prompt-header h2 { margin: 0 0 5px; font-size: 18px; color: #252b36; }
.endpoint-prompt-header p { margin: 0; color: #777; font-size: 12px; }
.endpoint-prompt-close { width: 32px; height: 32px; border: 0; border-radius: 50%; background: #f0f0f4; color: #444; font-size: 22px; line-height: 1; cursor: pointer; }
.endpoint-prompt-close:hover { background: #e94560; color: white; }
.endpoint-prompt-body { overflow: auto; padding: 16px 18px 4px; }
.prompt-modal-hint { margin: 0 0 10px; color: #555; font-size: 12px; line-height: 1.55; }
.endpoint-prompt-default { margin-bottom: 12px; padding: 8px 10px; border: 1px solid #e2e5eb; border-radius: 6px; background: #fafafd; }
.endpoint-prompt-default summary { cursor: pointer; color: #2a6496; font-size: 11px; }
.endpoint-prompt-default pre { max-height: 220px; overflow: auto; margin: 8px 0 0; padding: 9px; white-space: pre-wrap; color: #555; background: white; border-radius: 4px; font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.endpoint-prompt-field { margin-top: 12px; }
.endpoint-prompt-field textarea { min-height: 180px; box-sizing: border-box; }
.endpoint-prompt-actions { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 18px 16px; border-top: 1px solid #e5e5eb; }
.sm { padding: 5px 10px; font-size: 11px; }
.json-view { max-height: 650px; overflow: auto; margin: 0; padding: 16px; border-radius: 6px; background: #171725; color: #d8d8e5; font-size: 11px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
.project-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.project-card { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 18px; border: 1px solid #e2e5eb; border-radius: 8px; background: #fff; }
.project-card h2 { margin: 0 0 6px; font-size: 18px; color: #252b36; }
.project-card p { margin: 0 0 7px; color: #777; font-size: 13px; }
.project-card code { color: #666; font-size: 11px; overflow-wrap: anywhere; }
.empty-semantic, .empty-state { text-align: center; padding: 60px 20px; color: #888; }
.empty-semantic p { margin-bottom: 14px; }
/* ── ADR workbench ── */
.adr-workbench { display: grid; gap: 14px; margin-bottom: 20px; }
.adr-card { padding: 14px; border: 1px solid #e2e5eb; border-radius: 8px; background: #fafafd; }
.adr-card-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.adr-card-heading h3 { margin: 0 0 4px; font-size: 14px; color: #333; }
.adr-card-heading p { margin: 0; color: #888; font-size: 11px; line-height: 1.5; }
.adr-tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 10px; }
.adr-tab { border: 1px solid #d9dce3; background: white; color: #555; border-radius: 14px; padding: 5px 12px; font-size: 12px; cursor: pointer; }
.adr-tab.active { border-color: #e94560; background: #fff0f2; color: #c82d48; font-weight: 600; }
.adr-form { display: grid; gap: 8px; }
.adr-field { display: grid; gap: 4px; color: #555; font-size: 11px; }
.adr-field > span { font-weight: 600; }
.adr-field input[type="text"], .adr-field input[type="date"], .adr-field input[type="number"], .adr-field textarea { min-width: 0; border: 1px solid #cfd3da; border-radius: 5px; padding: 7px 8px; background: white; font: 12px/1.5 sans-serif; }
.adr-field textarea { resize: vertical; }
.adr-field select { min-width: 0; border: 1px solid #cfd3da; border-radius: 5px; padding: 7px 8px; background: white; font: 12px/1.5 sans-serif; }
.adr-sort-button { border: 0; background: transparent; padding: 0; font: inherit; font-weight: 600; color: inherit; cursor: pointer; }
.adr-sort-button:hover { color: #2b3a67; text-decoration: underline; }
.adr-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.adr-check { display: inline-flex; align-items: center; gap: 6px; color: #555; font-size: 12px; cursor: pointer; }
.adr-actions { display: flex; gap: 8px; margin-top: 12px; }
.adr-actions button { border: 0; border-radius: 6px; padding: 8px 14px; cursor: pointer; }
.adr-file-note { margin: 0; color: #2a6496; font-size: 11px; }
.adr-success { margin: 10px 0 0; padding: 8px 12px; border: 1px solid #bfe5cd; border-radius: 6px; background: #eef9f2; color: #23764a; font-size: 12px; }
.adr-success .link-button { margin-left: 6px; }
.adr-batch-note { margin: 6px 0 0; color: #555; }
.adr-member { color: #666; font-size: 11px; }
.adr-prompt-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 8px; color: #666; font-size: 11px; }
.adr-prompt-meta code { color: #2a6496; word-break: break-all; }
.adr-prompt-card summary { cursor: pointer; color: #333; font-size: 14px; font-weight: 600; }
.adr-prompt-body { margin-top: 12px; }
.adr-prompt-body textarea { width: 100%; box-sizing: border-box; }
.adr-detail { margin-top: 14px; padding: 12px; border: 1px solid #e2e5eb; border-radius: 7px; background: #fff; }
.adr-workbench > .adr-detail { margin-top: 0; }
.adr-detail-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.adr-detail-heading h4 { margin: 0; font-size: 13px; color: #333; }
.adr-detail-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 8px 0; color: #888; font-size: 11px; }
.adr-detail h5, .adr-debug h6 { margin: 12px 0 5px; font-size: 12px; color: #444; }
.adr-markdown { max-height: 420px; overflow: auto; margin: 0; padding: 10px; border: 1px solid #eee; border-radius: 5px; background: #fbfbfd; white-space: pre-wrap; word-break: break-word; font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.adr-debug { margin-top: 12px; border-top: 1px solid #eee; padding-top: 8px; font-size: 11px; }
.adr-debug > summary { cursor: pointer; color: #2a6496; font-weight: 600; }
.adr-debug pre { max-height: 260px; overflow: auto; margin: 4px 0 8px; padding: 8px; border-radius: 4px; background: #f5f6f8; white-space: pre-wrap; word-break: break-word; font: 10px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.adr-debug details { margin: 4px 0 8px; }
.adr-debug details summary { cursor: pointer; color: #555; }
.adr-row-actions { display: flex; flex-wrap: wrap; gap: 5px; }
.adr-row-actions button { border: 0; border-radius: 5px; padding: 4px 8px; font-size: 11px; cursor: pointer; }
.adr-row-actions .danger { background: #fcebed; color: #b8324a; }
.adr-exported { display: block; margin-top: 3px; color: #23764a; font-size: 10px; }
.adr-version { display: block; margin-top: 3px; color: #6b5b95; font-size: 10px; }
.adr-revisions { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.adr-revisions li { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 5px 8px; border: 1px solid #eee; border-radius: 5px; font-size: 11px; }
.adr-commit-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.adr-commit-progress { margin: 8px 0 10px; }
.adr-commit-filters { display: flex; flex-wrap: wrap; gap: 10px; margin: 6px 0 2px; }
.adr-commit-filters .adr-field { max-width: 170px; }
.adr-commit-pager { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 10px 0 4px; }
.adr-commit-pager-info { color: #555; font-size: 11px; font-variant-numeric: tabular-nums; }
.adr-page-number { min-width: 30px; justify-content: center; font-variant-numeric: tabular-nums; }
.adr-page-number.active { border-color: #4a6cf7; color: #2b3a67; font-weight: 600; background: #eef2ff; }
.adr-commit-pager select { border: 1px solid #cfd3da; border-radius: 5px; padding: 4px 6px; background: white; font: 11px/1.4 sans-serif; }
.adr-stage-filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 12px; }
.adr-stage-chip { display: inline-flex; align-items: center; gap: 5px; border: 1px solid #d7dbe3; border-radius: 12px; padding: 3px 9px; background: #fff; color: #555; font-size: 11px; cursor: pointer; }
.adr-stage-chip:hover { border-color: #b9c2d0; }
.adr-stage-chip.active { border-color: #2a6496; background: #eef7ff; color: #1f5c8c; }
.adr-stage-chip-count { border-radius: 8px; padding: 0 5px; background: #f0f1f5; color: #666; font-size: 10px; }
.adr-stage-chip.active .adr-stage-chip-count { background: #d9ebfb; color: #1f5c8c; }
.adr-stage-badge { display: inline-block; white-space: nowrap; }
.adr-stage-pending { background: #f0f1f5; color: #666; }
.adr-stage-queued { background: #eaf2fb; color: #2a6496; }
.adr-stage-generating { background: #fff4dc; color: #8a6413; }
.adr-stage-generated { background: #f1edfb; color: #5b4a9b; }
.adr-stage-backtested { background: #e8f4f6; color: #1e6b78; }
.adr-stage-approved { background: #e8f7ed; color: #23764a; }
.adr-stage-rejected { background: #fcebed; color: #b8324a; }
.adr-stage-exported { background: #dff3e6; color: #1d6b42; }
.adr-stage-skipped { background: #f0f1f5; color: #888; }
.adr-stage-failed { background: #fcebed; color: #b8324a; }
.adr-commit-summary { color: #666; font-size: 11px; }
.adr-commit-sha { color: #2a6496; font-size: 11px; }
.adr-commit-subject { min-width: 180px; }
.adr-select-cell { width: 30px; text-align: center; }
table[data-testid="adr-commit-table"] tr.skipped { color: #888; }
.adr-change-type { display: block; margin-top: 3px; color: #888; font-size: 10px; }
.adr-review-chip { display: inline-block; margin-left: 5px; border-radius: 9px; padding: 1px 7px; background: #f0f1f5; color: #666; font-size: 10px; }
.adr-review-chip.approved { background: #e7f6ed; color: #23764a; }
.adr-review-chip.rejected { background: #fcebed; color: #b8324a; }
.adr-review-chip.pending { background: #fff4dc; color: #8a6413; }
.adr-backtest-summary { display: inline-block; border-radius: 9px; padding: 1px 8px; background: #f0f1f5; color: #666; font-size: 11px; white-space: nowrap; }
.adr-backtest-summary.consistent { background: #e7f6ed; color: #23764a; }
.adr-backtest-summary.partial { background: #fff4dc; color: #8a6413; }
.adr-backtest-summary.contradicted { background: #fcebed; color: #b8324a; }
.adr-backtest-summary.unverifiable { background: #eef1f6; color: #4c5a72; }
.adr-detail-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 6px 0 4px; }
.adr-detail-section { margin-top: 13px; border-top: 1px solid #eee; padding-top: 9px; }
.adr-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.adr-section-heading h5 { margin: 0; }
.adr-backtest-verdict { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 6px 0; color: #555; font-size: 11px; }
.adr-confidence-track { height: 7px; margin: 6px 0 10px; overflow: hidden; border-radius: 5px; background: #eef0f4; }
.adr-confidence-track span { display: block; height: 100%; border-radius: inherit; background: #4a9d6d; transition: width .25s ease; }
.adr-reasons { margin: 0 0 8px; padding-left: 18px; color: #444; font-size: 11px; line-height: 1.55; }
.adr-review-status { margin: 6px 0; color: #555; font-size: 11px; }
.adr-review-form { display: grid; gap: 8px; max-width: 520px; }
.adr-review-form select { border: 1px solid #cfd3da; border-radius: 5px; padding: 7px 8px; background: white; font: 12px/1.5 sans-serif; }
.adr-review-form textarea { width: 100%; box-sizing: border-box; }
.adr-review-form .adr-field { max-width: 220px; }
tr.selected { background: #fff0f2; }
.adr-facts h3 { margin: 0 0 4px; font-size: 14px; color: #333; }
.adr-facts > p.muted { margin: 0 0 12px; }
@media (max-width: 900px) {
  .adr-grid { grid-template-columns: 1fr; }
  .adr-card-heading { display: block; }
  .adr-member { display: block; margin-top: 4px; }
}
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
  .api-explanation-actions { margin-top: 8px; justify-content: flex-start; }
  .endpoint-prompt-backdrop { padding: 0; }
  .endpoint-prompt-dialog { width: 100vw; height: 100vh; max-height: none; border-radius: 0; }
}
/* Phone layout: one readable column, thumb-sized controls and tables that
   scroll inside their panel instead of widening the page. */
@media (max-width: 720px) {
  .page-header { margin-bottom: 12px; }
  .page-header h1 { font-size: 20px; overflow-wrap: anywhere; }
  .page-header p { font-size: 12px; line-height: 1.5; }
  .page-header p code { display: inline-block; max-width: 100%; overflow-wrap: anywhere; }
  .actions { display: grid; grid-template-columns: 1fr; gap: 8px; }
  .actions button { width: 100%; min-height: 42px; }
  .summary-grid { gap: 8px; margin-bottom: 14px; }
  .summary-card { padding: 12px 13px; }
  .summary-value { font-size: 21px; }
  .summary-label { font-size: 11px; }
  .content { gap: 10px; }
  .section-nav { padding: 6px; }
  .section-nav button { min-width: 118px; padding: 8px 9px; font-size: 12px; }
  .section-nav small { font-size: 8px; }
  .panel { padding: 12px; }
  .panel-title { display: block; padding-bottom: 10px; margin-bottom: 12px; }
  .panel-title h2 { font-size: 17px; }
  .panel-title .phase { display: inline-block; margin-top: 8px; }
  .metrics { flex-wrap: wrap; gap: 8px 14px; }
  .metric { font-size: 21px; margin-bottom: 8px; }
  .metric small { font-size: 11px; }
  .table-wrap { min-width: 0; margin: 0 -4px; padding: 0 4px 4px; -webkit-overflow-scrolling: touch; }
  .adr-workbench, .adr-card, .adr-form, .adr-tabs { min-width: 0; }
  .table-wrap > table { min-width: 660px; }
  /* The API contract keeps its expandable detail usable by dropping the two
     summary columns that are repeated inside the expanded detail. */
  .table-wrap > table.api-contract-table { min-width: 0; table-layout: fixed; }
  .api-contract-table > thead > tr > th:nth-child(4),
  .api-contract-table > thead > tr > th:nth-child(5),
  .api-contract-table > tbody > tr > td:nth-child(4),
  .api-contract-table > tbody > tr > td:nth-child(5) { display: none; }
  .api-contract-table > thead > tr > th:nth-child(1) { width: 18%; }
  .api-contract-table > thead > tr > th:nth-child(2) { width: 32%; }
  .api-contract-table > thead > tr > th:nth-child(3) { width: 30%; }
  .api-contract-table > thead > tr > th:nth-child(6) { width: 20%; }
  .api-contract-table th, .api-contract-table td { padding: 8px 7px; white-space: normal; overflow-wrap: anywhere; }
  .api-contract-table td code { word-break: break-word; }
  .api-contract-table .expand-detail > td { padding: 8px 4px 12px; }
  th, td { padding: 8px 9px; }
  .table-tools { gap: 8px; }
  .table-tools label { width: 100%; }
  .table-tools input, .table-tools select { width: 100%; min-width: 0; }
  .table-tools > span { margin-left: 0; width: 100%; }
  .table-tools .explanation-status-summary { margin-left: 0; }
  .pagination { justify-content: center; flex-wrap: wrap; gap: 8px; }
  .pagination button { min-height: 38px; }
  .terms-toolbar { flex-direction: column; align-items: flex-start; gap: 8px; }
  .terms-toolbar .secondary { display: inline-flex; min-height: 38px; align-items: center; }
  .contract-grid { grid-template-columns: 1fr; }
  .contract-grid pre { max-height: 140px; }
  .call-chain span { font-size: 10px; }
  .seq-details summary { padding: 6px 0; }
  .seq-actions { justify-content: flex-start; }
  .api-prompt-panel, .batch-card { padding: 10px; }
  .api-prompt-heading, .batch-heading { display: block; }
  .api-prompt-heading > span { display: inline-block; margin-top: 4px; }
  .api-prompt-actions button { flex: 1 1 45%; min-height: 36px; }
  .batch-item { flex-wrap: wrap; gap: 4px; }
  .batch-item code { min-width: 0; overflow-wrap: anywhere; }
  .api-explanation-heading { display: block; }
  .api-explanation-actions { margin-top: 8px; justify-content: flex-start; }
  .api-explanation-actions button { flex: 1 1 45%; min-height: 36px; }
  .explanation-meta, .coverage-grid { gap: 6px; }
  .node-explanation header { flex-wrap: wrap; }
  .business-rule-section h4 { overflow-wrap: anywhere; }
  .json-view { padding: 10px; font-size: 10px; max-height: 520px; }
  .project-list { grid-template-columns: 1fr; gap: 12px; }
  .project-card { flex-direction: column; align-items: stretch; gap: 10px; padding: 14px; }
  .project-card .primary { width: 100%; text-align: center; }
  .module-tree { border-radius: 6px; }
  .tree-row { flex-wrap: wrap; gap: 4px 6px; min-height: 34px; padding-top: 5px; padding-bottom: 5px; }
  .tree-label { text-align: left; }
  .tree-meta { font-size: 10px; }
  .tree-file { word-break: break-all; }
  .adr-tabs { gap: 4px; }
  .adr-tab { padding: 6px 10px; font-size: 11px; }
  .adr-actions { flex-wrap: wrap; }
  .adr-actions button { flex: 1; min-height: 40px; }
  .adr-row-actions { flex-wrap: wrap; }
  .adr-row-actions button { min-height: 32px; }
  .table-wrap > table[data-testid="adr-other-drafts-table"] { min-width: 0; table-layout: fixed; }
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(3),
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(4),
  table[data-testid="adr-other-drafts-table"] > tbody > tr > td:nth-child(3),
  table[data-testid="adr-other-drafts-table"] > tbody > tr > td:nth-child(4) { display: none; }
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(1) { width: 32%; }
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(2) { width: 14%; }
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(5) { width: 18%; }
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(6) { width: 14%; }
  table[data-testid="adr-other-drafts-table"] > thead > tr > th:nth-child(7) { width: 22%; }
  table[data-testid="adr-other-drafts-table"] th,
  table[data-testid="adr-other-drafts-table"] td { padding: 8px 6px; }
  .adr-markdown, .adr-debug pre { max-height: 300px; }
  .sequence-zoom-dialog, .endpoint-prompt-dialog { height: 100dvh; }
  .sequence-zoom-header { padding: 12px 14px; }
  .sequence-zoom-header h2 { font-size: 16px; }
  .sequence-zoom-canvas { padding: 54px 8px 16px; }
  .sequence-zoom-controls { top: 8px; right: 8px; }
  .endpoint-prompt-header { padding: 14px; }
  .endpoint-prompt-body { padding: 12px 14px 4px; }
  .endpoint-prompt-actions { padding: 10px 14px calc(12px + env(safe-area-inset-bottom, 0px)); }
  .endpoint-prompt-actions button { flex: 1; min-height: 40px; }
  .empty-semantic, .empty-state { padding: 36px 12px; }
}
@media (max-width: 380px) {
  /* Keep the endpoint list readable on the narrowest phones; the explanation
     status remains visible inside the expanded endpoint detail. */
  .api-contract-table > thead > tr > th:nth-child(6),
  .api-contract-table > tbody > tr > td:nth-child(6) { display: none; }
  .api-contract-table > thead > tr > th:nth-child(1) { width: 20%; }
  .api-contract-table > thead > tr > th:nth-child(2) { width: 45%; }
  .api-contract-table > thead > tr > th:nth-child(3) { width: 35%; }
  .api-contract-table th:first-child, .api-contract-table td:first-child { white-space: nowrap; }
}
</style>
