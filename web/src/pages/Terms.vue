<template>
  <div class="terms-page">
    <UiState v-if="error" kind="error" :title="t('术语识别失败')" :message="error.message" :action-label="t('重试')" :dismiss-label="t('关闭')" @action="load" @dismiss="error = null" />
    <template v-else-if="viewId">
      <header class="terms-header">
        <div><h1>{{ t('跨服务术语对齐') }}</h1><p>{{ t('以 Graph View 确定范围，只对齐存在业务通信关系的服务。') }} <code>{{ viewId }}</code></p></div>
        <button class="primary" :disabled="alignmentLoading" @click="align">{{ alignmentLoading ? t('对齐中...') : t('生成对齐候选') }}</button>
      </header>
      <section class="panel alignment-scope">
        <div class="panel-heading"><div><h2>{{ t('推荐服务对') }}</h2><p>{{ t('默认排除网关、注册中心、配置和监控等技术服务。') }}</p></div><span>{{ t('人工审核后生效') }}</span></div>
        <div v-if="servicePairs.length" class="pair-list"><span v-for="pair in servicePairs" :key="pair.source_service_id + '-' + pair.target_service_id" class="pair"><code>{{ pair.source_service_id }}</code> ↔ <code>{{ pair.target_service_id }}</code><small>{{ pair.reasons.join(', ') }}</small></span></div>
        <p v-else class="empty">{{ t('暂无服务对，请先生成对齐候选。') }}</p>
        <p v-if="excludedServices.length" class="excluded">{{ t('已排除技术服务') }}：{{ excludedServices.map(item => item.service_id).join(', ') }}</p>
      </section>
      <section class="panel">
        <div class="panel-heading"><div><h2>{{ t('跨服务对齐候选') }}</h2><p>{{ t('same 也只是推荐结果，必须经过人工审核。') }}</p></div><span>{{ t('共 {count} 条', { count: alignments.length }) }}</span></div>
        <div class="table-wrap" v-if="alignments.length"><table><thead><tr><th>{{ t('服务') }}</th><th>{{ t('术语') }}</th><th>{{ t('关系') }}</th><th>{{ t('可信度') }}</th><th>{{ t('状态') }}</th><th>{{ t('操作') }}</th></tr></thead><tbody><tr v-for="item in alignments" :key="item.id"><td><code>{{ item.source_service_id }}</code> ↔ <code>{{ item.target_service_id }}</code></td><td>{{ item.source_term_id }} ↔ {{ item.target_term_id }}</td><td>{{ item.relationship }}</td><td><span class="score">{{ Math.round(Number(item.confidence || 0) * 100) }}%</span></td><td><span class="status" :class="item.status">{{ item.status }}</span></td><td class="row-actions"><button v-if="item.status !== 'accepted' && item.status !== 'rejected'" class="link" @click="reviewAlignment(item, 'accept')">{{ t('接纳') }}</button><button v-if="item.status !== 'rejected'" class="link danger" @click="reviewAlignment(item, 'reject')">{{ t('排除') }}</button></td></tr></tbody></table></div>
        <p v-else class="empty">{{ t('暂无对齐候选，请确认 Graph View 中存在业务服务调用。') }}</p>
      </section>
    </template>
    <UiState v-else-if="!snapshotId" kind="empty" :title="t('需要选择 Snapshot')" :message="t('请先在 Snapshots 中选择一个已发布快照，再识别术语。')" :action-label="t('前往 Snapshots')" @action="$router.push({ name: 'snapshots' })" />
    <template v-else>
      <header class="terms-header">
        <div><h1>{{ t('术语识别') }}</h1><p>{{ t('基于 API 与实体双锚点生成可追溯的术语清单。') }} <code>{{ snapshotId }}</code></p></div>
        <div class="actions"><button class="secondary" :disabled="loading" @click="extract">{{ loading ? t('识别中...') : t('重新识别') }}</button><button class="secondary" @click="manualOpen = !manualOpen">{{ t('人工补充') }}</button><button class="primary" :disabled="judging" @click="runPairJudgments">{{ judging ? t('归一化中...') : t('生成成对判断') }}</button></div>
      </header>

      <nav class="tabs">
        <button :class="{ active: panel === 'terms' }" @click="panel = 'terms'">{{ t('术语清单') }}</button>
        <button :class="{ active: panel === 'normalization' }" @click="showNormalization">{{ t('归一化') }}</button>
        <button :class="{ active: panel === 'releases' }" @click="showReleases">{{ t('词典发布') }}</button>
      </nav>

      <form v-if="manualOpen" class="manual-form" @submit.prevent="addManual">
        <label>{{ t('规范名') }}<input v-model.trim="manual.canonical_name" required /></label>
        <label>{{ t('类型') }}<select v-model="manual.term_type"><option v-for="type in termTypes" :key="type" :value="type">{{ termTypeLabel(type) }}</option></select></label>
        <label class="wide">{{ t('业务定义') }}<textarea v-model.trim="manual.definition" rows="2" /></label>
        <label>{{ t('作者') }}<input v-model.trim="manual.author" /></label>
        <button class="primary" :disabled="saving">{{ saving ? t('保存中...') : t('保存术语') }}</button>
      </form>

      <template v-if="panel === 'terms'">
        <div class="summary" v-if="summary">
          <div><strong>{{ summary.total }}</strong><span>{{ t('候选总数') }}</span></div>
          <div><strong>{{ summary.accepted }}</strong><span>{{ t('已接纳') }}</span></div>
          <div><strong>{{ summary.needs_review }}</strong><span>{{ t('待审核') }}</span></div>
          <div><strong>{{ summary.candidates }}</strong><span>{{ t('候选池') }}</span></div>
        </div>

        <section class="toolbar">
          <label>{{ t('状态') }}<select v-model="filters.status" @change="load"><option value="">{{ t('全部') }}</option><option value="accepted">accepted</option><option value="needs_review">needs_review</option><option value="candidate">candidate</option><option value="rejected">rejected</option></select></label>
          <label>{{ t('类型') }}<select v-model="filters.type" @change="load"><option value="">{{ t('全部') }}</option><option v-for="type in termTypes" :key="type" :value="type">{{ termTypeLabel(type) }}</option></select></label>
          <label class="search">{{ t('搜索术语') }}<input v-model.trim="filters.name" @input="debouncedLoad" /></label>
        </section>

        <section class="panel">
          <div class="panel-heading"><div><h2>{{ t('按可信度排序的术语清单') }}</h2><p>{{ t('默认清单优先保证准确率，候选池用于人工补充召回率。') }}</p></div><span>{{ t('共 {count} 条', { count: terms.length }) }}</span></div>
          <div class="table-wrap" v-if="terms.length"><table><thead><tr><th>#</th><th>{{ t('名称') }}</th><th>{{ t('类型') }}</th><th>{{ t('可信度') }}</th><th>{{ t('状态') }}</th><th>{{ t('来源') }}</th><th>{{ t('操作') }}</th></tr></thead><tbody>
            <template v-for="term in terms" :key="term.id"><tr :class="{ expanded: expanded === term.id }" @click="toggleEvidence(term)"><td>{{ term.rank || '-' }}</td><td><strong>{{ term.canonical_name }}</strong><small v-if="term.aliases?.length">{{ term.aliases.join(', ') }}</small></td><td><code>{{ termTypeLabel(term.term_type) }}</code></td><td><span class="score">{{ Math.round(Number(term.confidence_score || 0) * 100) }}%</span><small>{{ term.confidence_band }}</small></td><td><span class="status" :class="term.status">{{ term.status }}</span></td><td>{{ term.source }}</td><td class="row-actions"><button v-if="term.status !== 'accepted'" class="link" @click.stop="review(term, 'accept')">{{ term.status === 'rejected' ? t('恢复') : t('接纳') }}</button><button v-if="term.status !== 'rejected'" class="link danger" @click.stop="review(term, 'reject')">{{ t('排除') }}</button></td></tr>
            <tr v-if="expanded === term.id" class="evidence-row"><td colspan="7">
              <div v-if="evidenceLoading" class="muted">{{ t('加载证据中...') }}</div>
              <ul v-else><li v-for="item in evidence" :key="item.id"><code>{{ item.evidence_type }}</code> {{ item.evidence_value }} <small>{{ item.source_location?.file_path || '' }}:{{ item.source_location?.line || '' }}</small></li></ul>
              <div class="definition-box">
                <div class="definition-head"><strong>{{ t('业务定义') }}</strong><span v-if="definitions[term.id]" class="status" :class="definitions[term.id].status">{{ definitions[term.id].status }}</span><button class="link" :disabled="definitionLoading === term.id" @click.stop="generateDefinition(term)">{{ definitionLoading === term.id ? t('生成中...') : t('生成定义') }}</button></div>
                <template v-if="definitions[term.id]">
                  <p>{{ definitions[term.id].definition || t('暂无定义') }}</p>
                  <div class="grounding"><span :class="definitions[term.id].grounding?.passed ? 'ok' : 'bad'">{{ definitions[term.id].grounding?.passed ? t('接地校验通过') : t('接地校验未通过') }}</span><small>{{ t('可信度') }} {{ Math.round(Number(definitions[term.id].confidence || 0) * 100) }}%</small></div>
                  <div v-if="definitions[term.id].facts?.length"><strong>{{ t('事实') }}</strong><ul><li v-for="(fact, index) in definitions[term.id].facts" :key="'f' + index">{{ fact.claim }} <small>{{ (fact.evidence_ids || []).join(', ') }}</small></li></ul></div>
                  <div v-if="definitions[term.id].inferences?.length"><strong>{{ t('推断') }}</strong><ul><li v-for="(item, index) in definitions[term.id].inferences" :key="'i' + index">{{ item.claim }}</li></ul></div>
                  <div v-if="definitions[term.id].unknowns?.length"><strong>{{ t('未知') }}</strong><ul><li v-for="(item, index) in definitions[term.id].unknowns" :key="'u' + index">{{ item }}</li></ul></div>
                  <div class="row-actions" v-if="definitions[term.id].status === 'pending_review'"><button class="link" @click.stop="reviewDefinition('accept')">{{ t('接纳') }}</button><button class="link danger" @click.stop="reviewDefinition('reject')">{{ t('排除') }}</button></div>
                </template>
              </div>
            </td></tr></template>
          </tbody></table></div>
          <p v-else class="empty">{{ t('暂无术语，请先执行识别或人工补充。') }}</p>
        </section>
      </template>

      <template v-else-if="panel === 'normalization'">
        <section class="panel">
          <div class="panel-heading"><div><h2>{{ t('成对判断') }}</h2><p>{{ t('硬约束禁止自动合并；所有合并与撤销都必须留下审核记录。') }}</p></div><span>{{ t('共 {count} 条', { count: judgments.length }) }}</span></div>
          <div class="table-wrap" v-if="judgments.length"><table><thead><tr><th>{{ t('术语对') }}</th><th>{{ t('判断') }}</th><th>{{ t('可信度') }}</th><th>{{ t('约束') }}</th><th>{{ t('状态') }}</th><th>{{ t('操作') }}</th></tr></thead><tbody><tr v-for="item in judgments" :key="item.id"><td>{{ termName(item.source_term_id) }} ↔ {{ termName(item.target_term_id) }}</td><td><code>{{ item.judgment }}</code></td><td><span class="score">{{ Math.round(Number(item.confidence || 0) * 100) }}%</span></td><td><small v-if="item.constraints?.length">{{ item.constraints.map(c => c.code).join(', ') }}</small><small v-else>-</small></td><td><span class="status" :class="item.status">{{ item.status }}</span></td><td class="row-actions"><button v-if="item.status === 'proposed'" class="link" @click="reviewJudgment(item, 'accept')">{{ t('接纳') }}</button><button v-if="item.status === 'proposed'" class="link danger" @click="reviewJudgment(item, 'reject')">{{ t('排除') }}</button><button v-if="item.merge_allowed && item.status === 'accepted'" class="link" @click="mergeJudgment(item)">{{ t('合并') }}</button></td></tr></tbody></table></div>
          <p v-else class="empty">{{ t('暂无需归一化的术语对。') }}</p>
        </section>
        <section class="panel">
          <div class="panel-heading"><div><h2>{{ t('合并记录') }}</h2><p>{{ t('撤销采用补偿操作，不删除原始合并记录。') }}</p></div><span>{{ t('共 {count} 条', { count: merges.length }) }}</span></div>
          <div class="table-wrap" v-if="merges.length"><table><thead><tr><th>{{ t('操作') }}</th><th>{{ t('源术语') }}</th><th>{{ t('目标术语') }}</th><th>{{ t('审核人') }}</th><th>{{ t('状态') }}</th><th>{{ t('操作') }}</th></tr></thead><tbody><tr v-for="item in merges" :key="item.id"><td><code>{{ item.operation }}</code></td><td>{{ item.source_term_ids.map(termName).join(', ') }}</td><td>{{ termName(item.target_term_id) }}</td><td>{{ item.reviewer || '-' }}</td><td><span class="status" :class="item.status">{{ item.status }}</span></td><td class="row-actions"><button v-if="item.operation === 'MERGE' && item.status === 'applied'" class="link danger" @click="reverseMerge(item)">{{ t('撤销合并') }}</button></td></tr></tbody></table></div>
          <p v-else class="empty">{{ t('暂无合并记录。') }}</p>
        </section>
      </template>

      <template v-else>
        <section class="panel release-form-panel">
          <div class="panel-heading"><div><h2>{{ t('发布词典') }}</h2><p>{{ t('发布后快照不可修改；回滚通过发布新版本完成。') }}</p></div></div>
          <form class="release-form" @submit.prevent="createRelease">
            <label>{{ t('版本号') }}<input v-model.trim="release.version" required /></label>
            <label>{{ t('发布名称') }}<input v-model.trim="release.name" /></label>
            <label class="wide">{{ t('发布说明') }}<input v-model.trim="release.description" /></label>
            <label>{{ t('发布人') }}<input v-model.trim="release.created_by" /></label>
            <button class="primary" :disabled="publishing">{{ publishing ? t('发布中...') : t('发布词典') }}</button>
          </form>
        </section>
        <section class="panel">
          <div class="panel-heading"><div><h2>{{ t('发布历史') }}</h2><p>{{ t('每个版本保留完整内容与快照指纹。') }}</p></div><span>{{ t('共 {count} 条', { count: releases.length }) }}</span></div>
          <div class="table-wrap" v-if="releases.length"><table><thead><tr><th>{{ t('版本号') }}</th><th>{{ t('发布名称') }}</th><th>{{ t('条目数') }}</th><th>{{ t('快照指纹') }}</th><th>{{ t('发布时间') }}</th><th>{{ t('操作') }}</th></tr></thead><tbody><tr v-for="item in releases" :key="item.release_id"><td><code>{{ item.version }}</code></td><td>{{ item.release_name }}</td><td>{{ item.item_count }}</td><td><code>{{ (item.snapshot_hash || '').slice(0, 12) }}</code></td><td>{{ item.created_at }}</td><td><button class="link" @click="loadDiff(item)">{{ t('查看差异') }}</button></td></tr></tbody></table></div>
          <p v-else class="empty">{{ t('暂无发布版本。') }}</p>
        </section>
        <section class="panel" v-if="releaseDiff">
          <div class="panel-heading"><div><h2>{{ t('词典差异') }}</h2><p>{{ releaseDiff.release_id }} ↔ {{ releaseDiff.compare_to }}</p></div></div>
          <div class="diff-grid">
            <div><strong>{{ t('新增') }}（{{ releaseDiff.added?.length || 0 }}）</strong><ul><li v-for="item in releaseDiff.added || []" :key="item.item_id">{{ item.content?.canonical_name || item.item_id }}</li></ul></div>
            <div><strong>{{ t('删除') }}（{{ releaseDiff.removed?.length || 0 }}）</strong><ul><li v-for="item in releaseDiff.removed || []" :key="item.item_id">{{ item.content?.canonical_name || item.item_id }}</li></ul></div>
            <div><strong>{{ t('变更') }}（{{ releaseDiff.changed?.length || 0 }}）</strong><ul><li v-for="item in releaseDiff.changed || []" :key="item.release_item.item_id">{{ item.release_item.content?.canonical_name || item.release_item.item_id }}<small>{{ (item.fields || []).map(field => field.field).join(', ') }}</small></li></ul></div>
          </div>
        </section>
      </template>
    </template>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'
import { LOCALE_EVENT, locale, t } from '../i18n.js'

export default {
  components: { UiState },
  data() {
    return {
      snapshotId: this.$route.query.snapshot_id || '', viewId: this.$route.query.view_id || '', terms: [], summary: null, evidence: [], expanded: '',
      servicePairs: [], excludedServices: [], alignments: [], alignmentLoading: false,
      evidenceLoading: false, manualOpen: false, saving: false, locale: locale.value, loadTimer: null,
      panel: 'terms', judging: false, judgments: [], judgmentNames: {}, merges: [],
      definitions: {}, definitionLoading: '',
      releases: [], releaseDiff: null, publishing: false,
      release: { version: '', name: '', description: '', created_by: '' },
      filters: { status: '', type: '', name: '' },
      termTypes: ['ENTITY', 'BUSINESS_EVENT', 'BUSINESS_ACTION', 'BUSINESS_ATTRIBUTE', 'BUSINESS_ENUM', 'COMMAND', 'API_RESOURCE', 'DTO', 'TECHNICAL_COMPONENT', 'PAGE'],
      manual: { canonical_name: '', term_type: 'ENTITY', definition: '', author: '' },
    }
  },
  created() { window.addEventListener(LOCALE_EVENT, this.refreshLocale); this.load() },
  beforeUnmount() { window.removeEventListener(LOCALE_EVENT, this.refreshLocale); clearTimeout(this.loadTimer) },
  watch: {
    '$route.query.snapshot_id'(value) { this.snapshotId = value || ''; this.load() },
    '$route.query.view_id'(value) { this.viewId = value || ''; this.load() },
  },
  methods: {
    t,
    termTypeLabel(type) {
      const labels = {
        ENTITY: '实体', BUSINESS_EVENT: '业务事件', BUSINESS_ACTION: '业务动作',
        BUSINESS_ATTRIBUTE: '业务属性', BUSINESS_ENUM: '业务枚举', COMMAND: '命令',
        API_RESOURCE: 'API 资源', DTO: '数据传输对象', TECHNICAL_COMPONENT: '技术组件', PAGE: '页面',
      }
      return labels[type] ? this.t(labels[type]) : type || '-'
    },
    refreshLocale(event) { this.locale = event?.detail || locale.value },
    async load() {
      if (this.viewId) {
        await this.$runAsync(async () => {
          const data = await this.$api.get('/api/terms/alignments', { view_id: this.viewId, limit: 500 })
          this.alignments = data.alignments || []
        })
        return
      }
      if (!this.snapshotId) return
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/terms', { snapshot_id: this.snapshotId, status: this.filters.status, type: this.filters.type, name: this.filters.name, limit: 500 })
        this.terms = data.terms || []
        this.summary = { total: data.total || 0, accepted: this.terms.filter(item => item.status === 'accepted').length, needs_review: this.terms.filter(item => item.status === 'needs_review').length, candidates: this.terms.filter(item => item.status !== 'accepted').length }
      })
    },
    debouncedLoad() { clearTimeout(this.loadTimer); this.loadTimer = setTimeout(() => this.load(), 250) },
    async extract() {
      await this.$runAsync(async () => { await this.$api.request('/api/terms/extract', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId }) }); await this.load() })
    },
    async align() {
      this.alignmentLoading = true
      try {
        const data = await this.$api.request('/api/terms/align', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view_id: this.viewId }) })
        this.servicePairs = data.service_pairs || []
        this.excludedServices = data.excluded_services || []
        this.alignments = [...(data.mappings || []), ...(data.alternatives || [])]
      } finally { this.alignmentLoading = false }
    },
    async reviewAlignment(item, action) {
      await this.$runAsync(async () => {
        await this.$api.request('/api/terms/alignments/' + item.id + '/review?view_id=' + encodeURIComponent(this.viewId), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, author: 'web' }) })
        await this.load()
      })
    },
    termName(id) { return this.judgmentNames[id] || (this.terms.find(item => item.id === id) || {}).canonical_name || id },
    async showNormalization() {
      this.panel = 'normalization'
      await this.loadNormalization()
    },
    async loadNormalization() {
      await this.$runAsync(async () => {
        const [judged, merges] = await Promise.all([
          this.$api.get('/api/terms/pair-judgments', { snapshot_id: this.snapshotId, limit: 500 }),
          this.$api.get('/api/terms/merges', { snapshot_id: this.snapshotId }),
        ])
        this.judgments = judged.judgments || []
        this.merges = merges.merges || []
      })
    },
    async runPairJudgments() {
      this.judging = true
      try {
        await this.$runAsync(async () => {
          const data = await this.$api.request('/api/terms/pair-judgments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId }) })
          this.judgments = data.judgments || []
          this.judgmentNames = data.term_names || {}
          this.panel = 'normalization'
          const merges = await this.$api.get('/api/terms/merges', { snapshot_id: this.snapshotId })
          this.merges = merges.merges || []
        })
      } finally { this.judging = false }
    },
    async reviewJudgment(item, action) {
      await this.$runAsync(async () => {
        await this.$api.request(`/api/terms/pair-judgments/${item.id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, reviewer: 'web' }) })
        await this.loadNormalization()
      })
    },
    async mergeJudgment(item) {
      await this.$runAsync(async () => {
        await this.$api.request('/api/terms/merge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId, source_term_ids: [item.source_term_id], target_term_id: item.target_term_id, reason: item.judgment, reviewer: 'web' }) })
        await this.load()
        await this.loadNormalization()
      })
    },
    async reverseMerge(item) {
      await this.$runAsync(async () => {
        await this.$api.request(`/api/terms/merge/${item.id}/reverse`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'manual reverse', reviewer: 'web' }) })
        await this.load()
        await this.loadNormalization()
      })
    },
    async toggleEvidence(term) {
      if (this.expanded === term.id) { this.expanded = ''; return }
      this.expanded = term.id; this.evidence = []; this.evidenceLoading = true
      try {
        const [data, definition] = await Promise.all([
          this.$api.get(`/api/terms/${term.id}/evidence`, { snapshot_id: this.snapshotId }),
          this.$api.get(`/api/terms/${term.id}/definition`, { snapshot_id: this.snapshotId }).catch(() => null),
        ])
        this.evidence = data.evidence || []
        if (definition) this.definitions = { ...this.definitions, [term.id]: definition.definition }
      } finally { this.evidenceLoading = false }
    },
    async generateDefinition(term) {
      this.definitionLoading = term.id
      try {
        const data = await this.$api.request(`/api/terms/${term.id}/definition?snapshot_id=${encodeURIComponent(this.snapshotId)}`, { method: 'POST' })
        this.definitions = { ...this.definitions, [term.id]: data.definition }
      } finally { this.definitionLoading = '' }
    },
    async reviewDefinition(action) {
      const term = this.terms.find(item => item.id === this.expanded)
      const definition = term && this.definitions[term.id]
      if (!definition) return
      await this.$runAsync(async () => {
        const data = await this.$api.request(`/api/terms/definitions/${definition.id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, reviewer: 'web' }) })
        this.definitions = { ...this.definitions, [term.id]: data.definition }
      })
    },
    async showReleases() {
      this.panel = 'releases'
      await this.loadReleases()
    },
    async loadReleases() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/releases')
        this.releases = data.releases || []
      })
    },
    async createRelease() {
      this.publishing = true
      try {
        await this.$runAsync(async () => {
          await this.$api.request('/api/releases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId, ...this.release }) })
          this.release = { version: '', name: '', description: '', created_by: '' }
          await this.loadReleases()
        })
      } finally { this.publishing = false }
    },
    async loadDiff(item) {
      await this.$runAsync(async () => {
        this.releaseDiff = await this.$api.get(`/api/releases/${item.release_id}/diff`)
      })
    },
    async review(term, action) {
      await this.$runAsync(async () => { await this.$api.request(`/api/terms/${term.id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId, action, value: {}, author: 'web' }) }); await this.load() })
    },
    async addManual() {
      this.saving = true
      try { await this.$api.request('/api/terms/manual', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId, ...this.manual }) }); this.manual = { canonical_name: '', term_type: 'ENTITY', definition: '', author: '' }; this.manualOpen = false; await this.load() } finally { this.saving = false }
    },
  },
}
</script>

<style scoped>
.terms-header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:18px; }.terms-header h1 { margin:0 0 5px; }.terms-header p,.panel-heading p { margin:0; color:#777; font-size:13px; }.actions { display:flex; gap:8px; flex-wrap:wrap; }.primary,.secondary { border-radius:6px; padding:8px 13px; cursor:pointer; }.primary { border:0; background:#e94560; color:#fff; }.secondary { border:1px solid #cdd2dc; background:#fff; color:#444; }.tabs { display:flex; gap:6px; margin-bottom:14px; }.tabs button { border:1px solid #e1e4ea; background:#fff; color:#555; border-radius:6px; padding:7px 12px; cursor:pointer; }.tabs button.active { border-color:#e94560; color:#e94560; background:#fff5f7; }.manual-form,.toolbar,.panel,.summary,.release-form { background:#fff; border:1px solid #e7e7eb; border-radius:8px; }.manual-form { display:grid; grid-template-columns:1fr 180px 1fr 160px auto; gap:10px; padding:14px; margin-bottom:16px; align-items:end; }.manual-form label,.toolbar label,.release-form label { display:grid; gap:5px; color:#555; font-size:12px; }.manual-form input,.manual-form select,.manual-form textarea,.toolbar input,.toolbar select,.release-form input { border:1px solid #cfd3da; border-radius:5px; padding:7px 8px; min-width:0; }.manual-form .wide { grid-column:auto; }.summary { display:flex; margin-bottom:16px; }.summary div { flex:1; padding:13px 16px; border-right:1px solid #eee; }.summary div:last-child { border:0; }.summary strong,.summary span { display:block; }.summary strong { font-size:21px; color:#e94560; }.summary span { color:#777; font-size:12px; margin-top:3px; }.toolbar { display:flex; gap:12px; padding:12px; margin-bottom:12px; }.toolbar .search { flex:1; }.panel { overflow:hidden; margin-bottom:16px; }.panel-heading { display:flex; justify-content:space-between; padding:16px; border-bottom:1px solid #eee; }.panel-heading h2 { margin:0 0 4px; font-size:17px; }.panel-heading > span { color:#888; font-size:12px; }.table-wrap { overflow:auto; }table { width:100%; border-collapse:collapse; font-size:13px; }th,td { padding:10px 12px; border-bottom:1px solid #eee; text-align:left; vertical-align:top; }th { color:#777; font-size:11px; }.clickable,tr:not(.evidence-row) { cursor:pointer; }tr.expanded,tr:hover { background:#fff9fa; }td small { display:block; color:#999; margin-top:3px; }.score { color:#1a8050; font-weight:700; }.status { padding:3px 6px; border-radius:10px; font-size:11px; background:#f0f0f5; }.status.accepted,.status.approved { background:#e8f7ed; color:#23764a; }.status.needs_review,.status.pending_review,.status.proposed { background:#fff3db; color:#8a6418; }.status.rejected { background:#fcebed; color:#b8324a; }.row-actions { white-space:nowrap; }.link { border:0; background:transparent; color:#2670a6; cursor:pointer; padding:0 6px 0 0; }.link.danger { color:#b8324a; }.evidence-row td { background:#fafafd; }.evidence-row ul { margin:0; padding-left:20px; }.definition-box { border-top:1px dashed #e1e4ea; margin-top:10px; padding-top:10px; }.definition-head { display:flex; gap:10px; align-items:center; margin-bottom:6px; }.grounding { display:flex; gap:12px; margin:6px 0; font-size:12px; }.grounding .ok { color:#23764a; }.grounding .bad { color:#b8324a; }.pair-list { display:flex; gap:10px; flex-wrap:wrap; padding:14px 16px; }.pair { padding:8px 10px; border:1px solid #e7e7eb; border-radius:6px; background:#fafafd; }.pair small { display:block; color:#888; margin-top:4px; }.excluded { margin:0; padding:0 16px 14px; color:#8a6418; font-size:12px; }.empty,.muted { color:#888; padding:28px; text-align:center; }.release-form { display:grid; grid-template-columns:160px 1fr 2fr 160px auto; gap:10px; padding:14px; align-items:end; border:0; border-radius:0; }.diff-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; padding:16px; }.diff-grid strong { display:block; margin-bottom:6px; }.diff-grid ul { margin:0; padding-left:18px; }.diff-grid small { display:block; color:#999; }@media (max-width:800px) { .terms-header,.toolbar { flex-direction:column; }.manual-form,.release-form { grid-template-columns:1fr; }.manual-form .wide { grid-column:auto; }.summary { flex-wrap:wrap; }.summary div { min-width:50%; }.actions { width:100%; }.actions button { flex:1; }.diff-grid { grid-template-columns:1fr; } }
</style>
