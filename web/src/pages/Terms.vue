<template>
  <div class="terms-page">
    <UiState v-if="error" kind="error" :title="t('术语识别失败')" :message="error.message" :action-label="t('重试')" :dismiss-label="t('关闭')" @action="load" @dismiss="error = null" />
    <UiState v-else-if="!snapshotId" kind="empty" :title="t('需要选择 Snapshot')" :message="t('请先在 Snapshots 中选择一个已发布快照，再识别术语。')" :action-label="t('前往 Snapshots')" @action="$router.push({ name: 'snapshots' })" />
    <template v-else>
      <header class="terms-header">
        <div><h1>{{ t('术语识别') }}</h1><p>{{ t('基于 API 与实体双锚点生成可追溯的术语清单。') }} <code>{{ snapshotId }}</code></p></div>
        <div class="actions"><button class="secondary" :disabled="loading" @click="extract">{{ loading ? t('识别中...') : t('重新识别') }}</button><button class="primary" @click="manualOpen = !manualOpen">{{ t('人工补充') }}</button></div>
      </header>

      <form v-if="manualOpen" class="manual-form" @submit.prevent="addManual">
        <label>{{ t('规范名') }}<input v-model.trim="manual.canonical_name" required /></label>
        <label>{{ t('类型') }}<select v-model="manual.term_type"><option value="entity">entity</option><option value="value_object">value_object</option><option value="state">state</option><option value="event">event</option><option value="action">action</option></select></label>
        <label class="wide">{{ t('业务定义') }}<textarea v-model.trim="manual.definition" rows="2" /></label>
        <label>{{ t('作者') }}<input v-model.trim="manual.author" /></label>
        <button class="primary" :disabled="saving">{{ saving ? t('保存中...') : t('保存术语') }}</button>
      </form>

      <div class="summary" v-if="summary">
        <div><strong>{{ summary.total }}</strong><span>{{ t('候选总数') }}</span></div>
        <div><strong>{{ summary.accepted }}</strong><span>{{ t('已接纳') }}</span></div>
        <div><strong>{{ summary.needs_review }}</strong><span>{{ t('待审核') }}</span></div>
        <div><strong>{{ summary.candidates }}</strong><span>{{ t('候选池') }}</span></div>
      </div>

      <section class="toolbar">
        <label>{{ t('状态') }}<select v-model="filters.status" @change="load"><option value="">{{ t('全部') }}</option><option value="accepted">accepted</option><option value="needs_review">needs_review</option><option value="candidate">candidate</option><option value="rejected">rejected</option></select></label>
        <label>{{ t('类型') }}<select v-model="filters.type" @change="load"><option value="">{{ t('全部') }}</option><option value="entity">entity</option><option value="resource">resource</option><option value="action">action</option><option value="value_object">value_object</option><option value="state">state</option><option value="event">event</option></select></label>
        <label class="search">{{ t('搜索术语') }}<input v-model.trim="filters.name" @input="debouncedLoad" /></label>
      </section>

      <section class="panel">
        <div class="panel-heading"><div><h2>{{ t('按可信度排序的术语清单') }}</h2><p>{{ t('默认清单优先保证准确率，候选池用于人工补充召回率。') }}</p></div><span>{{ t('共 {count} 条', { count: terms.length }) }}</span></div>
        <div class="table-wrap" v-if="terms.length"><table><thead><tr><th>#</th><th>{{ t('名称') }}</th><th>{{ t('类型') }}</th><th>{{ t('可信度') }}</th><th>{{ t('状态') }}</th><th>{{ t('来源') }}</th><th>{{ t('操作') }}</th></tr></thead><tbody>
          <template v-for="term in terms" :key="term.id"><tr :class="{ expanded: expanded === term.id }" @click="toggleEvidence(term)"><td>{{ term.rank || '-' }}</td><td><strong>{{ term.canonical_name }}</strong><small v-if="term.aliases?.length">{{ term.aliases.join(', ') }}</small></td><td><code>{{ term.term_type }}</code></td><td><span class="score">{{ Math.round(Number(term.confidence_score || 0) * 100) }}%</span><small>{{ term.confidence_band }}</small></td><td><span class="status" :class="term.status">{{ term.status }}</span></td><td>{{ term.source }}</td><td class="row-actions"><button v-if="term.status !== 'accepted' && term.status !== 'rejected'" class="link" @click.stop="review(term, 'accept')">{{ t('接纳') }}</button><button v-if="term.status !== 'rejected'" class="link danger" @click.stop="review(term, 'reject')">{{ t('排除') }}</button></td></tr><tr v-if="expanded === term.id" class="evidence-row"><td colspan="7"><div v-if="evidenceLoading" class="muted">{{ t('加载证据中...') }}</div><ul v-else><li v-for="item in evidence" :key="item.id"><code>{{ item.evidence_type }}</code> {{ item.evidence_value }} <small>{{ item.source_location?.file_path || '' }}:{{ item.source_location?.line || '' }}</small></li></ul></td></tr></template>
        </tbody></table></div>
        <p v-else class="empty">{{ t('暂无术语，请先执行识别或人工补充。') }}</p>
      </section>
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
      snapshotId: this.$route.query.snapshot_id || '', terms: [], summary: null, evidence: [], expanded: '',
      evidenceLoading: false, manualOpen: false, saving: false, locale: locale.value, loadTimer: null,
      filters: { status: '', type: '', name: '' },
      manual: { canonical_name: '', term_type: 'entity', definition: '', author: '' },
    }
  },
  created() { window.addEventListener(LOCALE_EVENT, this.refreshLocale); this.load() },
  beforeUnmount() { window.removeEventListener(LOCALE_EVENT, this.refreshLocale); clearTimeout(this.loadTimer) },
  watch: { '$route.query.snapshot_id'(value) { this.snapshotId = value || ''; this.load() } },
  methods: {
    t,
    refreshLocale(event) { this.locale = event?.detail || locale.value },
    async load() {
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
    async toggleEvidence(term) {
      if (this.expanded === term.id) { this.expanded = ''; return }
      this.expanded = term.id; this.evidence = []; this.evidenceLoading = true
      try { const data = await this.$api.get(`/api/terms/${term.id}/evidence`, { snapshot_id: this.snapshotId }); this.evidence = data.evidence || [] } finally { this.evidenceLoading = false }
    },
    async review(term, action) {
      await this.$runAsync(async () => { await this.$api.request(`/api/terms/${term.id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId, action, value: {}, author: 'web' }) }); await this.load() })
    },
    async addManual() {
      this.saving = true
      try { await this.$api.request('/api/terms/manual', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: this.snapshotId, ...this.manual }) }); this.manual = { canonical_name: '', term_type: 'entity', definition: '', author: '' }; this.manualOpen = false; await this.load() } finally { this.saving = false }
    },
  },
}
</script>

<style scoped>
.terms-header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:18px; }.terms-header h1 { margin:0 0 5px; }.terms-header p,.panel-heading p { margin:0; color:#777; font-size:13px; }.actions { display:flex; gap:8px; }.primary,.secondary { border-radius:6px; padding:8px 13px; cursor:pointer; }.primary { border:0; background:#e94560; color:#fff; }.secondary { border:1px solid #cdd2dc; background:#fff; color:#444; }.manual-form,.toolbar,.panel,.summary { background:#fff; border:1px solid #e7e7eb; border-radius:8px; }.manual-form { display:grid; grid-template-columns:1fr 180px 1fr 160px auto; gap:10px; padding:14px; margin-bottom:16px; align-items:end; }.manual-form label,.toolbar label { display:grid; gap:5px; color:#555; font-size:12px; }.manual-form input,.manual-form select,.manual-form textarea,.toolbar input,.toolbar select { border:1px solid #cfd3da; border-radius:5px; padding:7px 8px; min-width:0; }.manual-form .wide { grid-column:auto; }.summary { display:flex; margin-bottom:16px; }.summary div { flex:1; padding:13px 16px; border-right:1px solid #eee; }.summary div:last-child { border:0; }.summary strong,.summary span { display:block; }.summary strong { font-size:21px; color:#e94560; }.summary span { color:#777; font-size:12px; margin-top:3px; }.toolbar { display:flex; gap:12px; padding:12px; margin-bottom:12px; }.toolbar .search { flex:1; }.panel { overflow:hidden; }.panel-heading { display:flex; justify-content:space-between; padding:16px; border-bottom:1px solid #eee; }.panel-heading h2 { margin:0 0 4px; font-size:17px; }.panel-heading > span { color:#888; font-size:12px; }.table-wrap { overflow:auto; }table { width:100%; border-collapse:collapse; font-size:13px; }th,td { padding:10px 12px; border-bottom:1px solid #eee; text-align:left; vertical-align:top; }th { color:#777; font-size:11px; }.clickable,tr:not(.evidence-row) { cursor:pointer; }tr.expanded,tr:hover { background:#fff9fa; }td small { display:block; color:#999; margin-top:3px; }.score { color:#1a8050; font-weight:700; }.status { padding:3px 6px; border-radius:10px; font-size:11px; background:#f0f0f5; }.status.accepted { background:#e8f7ed; color:#23764a; }.status.needs_review { background:#fff3db; color:#8a6418; }.status.rejected { background:#fcebed; color:#b8324a; }.row-actions { white-space:nowrap; }.link { border:0; background:transparent; color:#2670a6; cursor:pointer; }.link.danger { color:#b8324a; }.evidence-row td { background:#fafafd; }.evidence-row ul { margin:0; padding-left:20px; }.empty,.muted { color:#888; padding:28px; text-align:center; }@media (max-width:800px) { .terms-header,.toolbar { flex-direction:column; }.manual-form { grid-template-columns:1fr; }.manual-form .wide { grid-column:auto; }.summary { flex-wrap:wrap; }.summary div { min-width:50%; }.actions { width:100%; }.actions button { flex:1; } }
</style>
