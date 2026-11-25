<template>
  <div class="snapshots-page">
    <div class="page-header">
      <div><h1>{{ t('Snapshots') }}</h1><p>{{ t('选择仓库成员后运行分析；分析会自动初始化并同步 CodeGraph。') }}</p></div>
      <div class="actions"><button class="secondary" :disabled="loading" @click="load">{{ t('刷新') }}</button><button class="secondary" :disabled="loading || runBusy || !selectedMemberIds.length" @click="createRun">{{ t('运行分析') }}</button><button class="primary" :disabled="loading || !selectedMemberIds.length" @click="createView">{{ t('创建当前 View') }}</button></div>
    </div>
    <UiState v-if="error" kind="error" :title="t('快照加载失败')" :message="error.message" :action-label="t('重试')" @action="load" />
    <UiState v-else-if="loading" kind="loading" :title="t('正在加载快照')" />
    <div v-else class="snapshot-scopes">
      <div v-if="view" class="view-card"><b>{{ t('当前 View') }}</b> <code>{{ view.id }}</code> <small>{{ view.digest }}</small><router-link :to="{ name: 'graph-view', params: { viewId: view.id } }">{{ t('查看跨仓图谱') }}</router-link><a :href="`/api/graph-views/${view.id}/export`" target="_blank">{{ t('导出') }}</a></div>

      <section v-if="activeRun" class="run-card" data-testid="analysis-run">
        <div class="run-header">
          <div><b>{{ t('分析 Run') }}</b> <code>{{ activeRun.id }}</code><span class="run-status" :class="`status-${activeRun.status}`">{{ runStatusText(activeRun.status) }}</span></div>
          <div class="run-actions">
            <button v-if="isActiveRun" class="secondary sm" :disabled="runBusy" @click="cancelRun">{{ t('取消') }} Run</button>
            <button v-if="retryableMemberIds.length" class="secondary sm" :disabled="runBusy" @click="retryFailed">{{ t('重试失败成员（{count}）', { count: retryableMemberIds.length }) }}</button>
          </div>
        </div>
        <p v-if="isActiveRun" class="muted">{{ t('正在轮询成员进度；离开后重新打开本页可继续查看此 Run。') }}</p>
        <p v-else-if="activeRun.status === 'partial'" class="muted">{{ t('部分成员未完成；已发布的 Snapshot 保持可用。') }}</p>
        <div v-if="isMobile" class="run-member-cards">
          <article v-for="member in activeRun.members || []" :key="member.member_id" class="run-member-card">
            <div class="run-member-head">
              <b>{{ memberName(member.member_id) }}</b>
              <span :class="`status-${member.attempt?.status || member.disposition}`">{{ attemptStatusText(member.attempt?.status || member.disposition) }}</span>
            </div>
            <p><span>{{ t('阶段') }}</span>{{ stageText(member.attempt?.stage) }}</p>
            <p class="run-member-detail"><span>{{ t('进度/错误') }}</span>{{ attemptDetail(member.attempt) }}</p>
            <button v-if="canCancelMember(member)" class="secondary sm" :disabled="runBusy" @click="cancelMember(member.member_id)">{{ t('取消') }}</button>
          </article>
        </div>
        <div v-else class="table-wrap">
          <table class="run-members"><thead><tr><th>{{ t('成员') }}</th><th>{{ t('状态') }}</th><th>{{ t('阶段') }}</th><th>{{ t('进度/错误') }}</th><th></th></tr></thead>
            <tbody><tr v-for="member in activeRun.members || []" :key="member.member_id">
              <td>{{ memberName(member.member_id) }}</td>
              <td><span :class="`status-${member.attempt?.status || member.disposition}`">{{ attemptStatusText(member.attempt?.status || member.disposition) }}</span></td>
              <td>{{ stageText(member.attempt?.stage) }}</td>
              <td class="attempt-detail">{{ attemptDetail(member.attempt) }}</td>
              <td><button v-if="canCancelMember(member)" class="secondary sm" :disabled="runBusy" @click="cancelMember(member.member_id)">{{ t('取消') }}</button></td>
            </tr></tbody>
          </table>
        </div>
      </section>

      <section v-for="scope in scopes" :key="scope.id" class="snapshot-scope">
        <h2>{{ scope.name }}</h2>
        <div v-if="isMobile" class="member-cards">
          <label class="member-select-all">
            <input :aria-label="t('选择 {name} 全部成员', { name: scope.name })" type="checkbox" :checked="scopeSelected(scope)" :indeterminate.prop="scopeIndeterminate(scope)" @change="toggleScope(scope, $event.target.checked)">
            <span>{{ t('选择 {name} 全部成员', { name: scope.name }) }}</span>
          </label>
          <article v-for="member in scope.members" :key="member.id" class="member-card">
            <div class="member-card-head">
              <label>
                <input v-model="selectedIds" type="checkbox" :value="member.id" :aria-label="t('选择 {name}', { name: member.display_name })" @change="rememberMemberSnapshot(member)">
                <b>{{ member.display_name }}</b>
                <small v-if="(member.declared_aliases || []).length" class="member-aliases">{{ member.declared_aliases.join(', ') }}</small>
              </label>
              <span class="member-card-state" :class="{ unparsed: !member.current_snapshot_id }">{{ member.current_snapshot_id ? t('已发布') : t('未解析') }}</span>
            </div>
            <p class="member-card-snapshot"><span>{{ t('当前 Snapshot') }}</span><code>{{ member.current_snapshot_id || '—' }}</code></p>
            <p v-if="member.check" data-testid="change-status" class="member-card-check" :class="`status-${member.check.status}`">{{ changeStatusText(member.check.status) }}</p>
            <div class="member-actions">
              <router-link v-if="member.current_snapshot_id" class="primary sm" :to="knowledgeLink(member.current_snapshot_id, member.display_name)" @click="rememberMemberSnapshot(member)">{{ t('查看知识') }}</router-link>
              <button v-if="member.current_snapshot_id" class="secondary sm" :disabled="checkingIds[member.id]" @click="checkMember(member)">{{ checkingIds[member.id] ? t('检查中...') : t('检查改动') }}</button>
              <button class="secondary sm" data-testid="member-config" @click="openMemberConfig(member)">{{ t('通信配置') }}</button>
            </div>
          </article>
        </div>
        <div v-else class="table-wrap">
          <table><thead><tr><th><input :aria-label="t('选择 {name} 全部成员', { name: scope.name })" type="checkbox" :checked="scopeSelected(scope)" :indeterminate.prop="scopeIndeterminate(scope)" @change="toggleScope(scope, $event.target.checked)"></th><th>{{ t('成员') }}</th><th>{{ t('当前 Snapshot') }}</th><th>{{ t('状态') }}</th><th></th></tr></thead>
            <tbody><tr v-for="member in scope.members" :key="member.id">
              <td><input v-model="selectedIds" type="checkbox" :value="member.id" :aria-label="t('选择 {name}', { name: member.display_name })" @change="rememberMemberSnapshot(member)"></td>
              <td>{{ member.display_name }}<small v-if="(member.declared_aliases || []).length" class="member-aliases">{{ member.declared_aliases.join(', ') }}</small></td>
              <td><code>{{ member.current_snapshot_id || '—' }}</code></td>
              <td>{{ member.current_snapshot_id ? t('已发布') : t('未解析') }}<span v-if="member.check" data-testid="change-status" :class="`status-${member.check.status}`"> · {{ changeStatusText(member.check.status) }}</span></td>
              <td class="member-actions"><router-link v-if="member.current_snapshot_id" class="primary sm" :to="knowledgeLink(member.current_snapshot_id, member.display_name)" @click="rememberMemberSnapshot(member)">{{ t('查看知识') }}</router-link><button v-if="member.current_snapshot_id" class="secondary sm" :disabled="checkingIds[member.id]" @click="checkMember(member)">{{ checkingIds[member.id] ? t('检查中...') : t('检查改动') }}</button><button class="secondary sm" data-testid="member-config" @click="openMemberConfig(member)">{{ t('通信配置') }}</button></td>
            </tr></tbody>
          </table>
        </div>
      </section>
      <p v-if="!scopes.length" class="muted">{{ t('暂无分析 Scope。') }}</p>
    </div>

    <div v-if="configMember" class="member-config-backdrop" role="dialog" aria-modal="true" aria-labelledby="member-config-title" @click.self="closeMemberConfig">
      <section class="member-config-dialog" @keydown.esc="closeMemberConfig">
        <header><h2 id="member-config-title">{{ t('成员通信配置') }}</h2><button class="secondary sm" @click="closeMemberConfig">{{ t('关闭') }}</button></header>
        <p class="muted">{{ t('配置 {name} 的服务主机别名与路由前缀；保存后新建的 View 才会生效。', { name: configMember.display_name }) }}</p>
        <label>
          <span>{{ t('服务别名（逗号分隔）') }}</span>
          <input v-model="configAliases" data-testid="member-aliases-input" :placeholder="t('例如 orders.internal, orders')" />
          <small>{{ t('用于把调用 URL 的 host 唯一映射到该成员；未声明时不确认跨服务边。') }}</small>
        </label>
        <label>
          <span>{{ t('路由基础路径') }}</span>
          <input v-model="configBasePath" data-testid="member-base-path-input" :placeholder="t('例如 /svc/orders')" />
          <small>{{ t('作为该成员所有入站路由的前缀参与匹配，留空表示无前缀。') }}</small>
        </label>
        <p v-if="configNotice" class="config-notice">{{ configNotice }}</p>
        <p v-if="configError" class="config-error">{{ configError }}</p>
        <div class="config-actions">
          <button class="secondary" @click="closeMemberConfig">{{ t('取消') }}</button>
          <button class="primary" data-testid="member-config-save" :disabled="configSaving" @click="saveMemberConfig">{{ configSaving ? t('保存中...') : t('保存配置') }}</button>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'
import { readNavigationContext, rememberNavigationContext } from '../navigationContext.js'
import { t } from '../i18n.js'

const ACTIVE_RUN_STATUSES = ['pending', 'running']
const RETRYABLE_ATTEMPT_STATUSES = ['failed', 'cancelled', 'interrupted']
const POLL_INTERVAL_MS = 1000
const LAST_RUN_KEY = 'codeevolution:last-analysis-run-id'

export default {
  components: { UiState },
  data() { return { scopes: [], selectedIds: [], loading: false, error: null, view: null, activeRun: null, runBusy: false, pollTimer: null, checkingIds: {}, isMobile: false, configMember: null, configAliases: '', configBasePath: '', configSaving: false, configError: null, configNotice: '' } },
  computed: {
    selectedMemberIds() { return this.selectedIds.filter(id => this.memberIds.includes(id)) },
    memberIds() { return this.scopes.flatMap(scope => scope.members.map(member => member.id)) },
    isActiveRun() { return ACTIVE_RUN_STATUSES.includes(this.activeRun?.status) },
    retryableMemberIds() { return (this.activeRun?.members || []).filter(member => RETRYABLE_ATTEMPT_STATUSES.includes(member.attempt?.status)).map(member => member.member_id) },
  },
  async created() {
    this.syncViewport()
    window.addEventListener('resize', this.syncViewport)
    await this.load()
    const runId = window.sessionStorage?.getItem(LAST_RUN_KEY)
    if (runId) await this.loadRun(runId, { quiet: true })
  },
  beforeUnmount() {
    this.stopPolling()
    window.removeEventListener('resize', this.syncViewport)
  },
  methods: {
    t,
    syncViewport() { this.isMobile = window.innerWidth <= 720 },
    async load() {
      this.loading = true; this.error = null
      try {
        const data = await this.$api.get('/api/scopes')
        const scopes = []
        for (const scope of data.scopes || []) {
          const members = await this.$api.get(`/api/scopes/${encodeURIComponent(scope.id)}/members`)
          const enriched = []
          for (const member of members.members || []) {
            const snapshots = await this.$api.get(`/api/repository-members/${encodeURIComponent(member.id)}/snapshots`, { limit: 1 })
            enriched.push({ ...member, current_snapshot_id: snapshots.items?.[0]?.id || null })
          }
          scopes.push({ ...scope, members: enriched })
        }
        this.scopes = scopes
        this.selectedIds = this.selectedIds.filter(id => this.memberIds.includes(id))
        const members = scopes.flatMap(scope => scope.members)
        const context = readNavigationContext()
        const current = members.find(member => member.current_snapshot_id === context.snapshotId)
          || members.find(member => member.current_snapshot_id)
        if (current && current.current_snapshot_id !== context.snapshotId) {
          rememberNavigationContext({ snapshotId: current.current_snapshot_id, snapshotMember: current.display_name })
        }
      } catch (error) { this.error = error } finally { this.loading = false }
    },
    knowledgeLink(snapshotId, memberName) { return { name: 'knowledge', params: { repoName: memberName || 'snapshot' }, query: { snapshot_id: snapshotId } } },
    rememberMemberSnapshot(member) {
      if (member?.current_snapshot_id) rememberNavigationContext({ snapshotId: member.current_snapshot_id, snapshotMember: member.display_name })
    },
    changeStatusText(status) { return this.t(({ unchanged: '未变化', source_changed: '现场已变化', analyzer_outdated: '分析器已更新', unparsed: '未解析', check_failed: '检查失败' })[status] || status || '未知') },
    async checkMember(member) {
      this.checkingIds[member.id] = true
      try {
        const response = await this.$api.request('/api/repository-members/check', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ member_ids: [member.id] }),
        })
        member.check = response.items?.[0] || { status: 'check_failed', error_message: this.t('检查未返回结果') }
      } catch (error) {
        member.check = { status: 'check_failed', error_message: error.message || this.t('检查失败') }
      } finally {
        delete this.checkingIds[member.id]
      }
    },
    memberName(memberId) { return this.scopes.flatMap(scope => scope.members).find(member => member.id === memberId)?.display_name || memberId },
    openMemberConfig(member) {
      this.configMember = member
      this.configAliases = (member.declared_aliases || []).join(', ')
      this.configBasePath = member.base_path || ''
      this.configError = null
      this.configNotice = ''
    },
    closeMemberConfig() {
      if (this.configSaving) return
      this.configMember = null
      this.configError = null
      this.configNotice = ''
    },
    async saveMemberConfig() {
      if (!this.configMember) return
      this.configSaving = true; this.configError = null; this.configNotice = ''
      try {
        const aliases = this.configAliases.split(',').map(item => item.trim()).filter(Boolean)
        const response = await this.$api.request(`/api/repository-members/${encodeURIComponent(this.configMember.id)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ declared_aliases: aliases, base_path: this.configBasePath.trim() }),
        })
        const updated = response.member || {}
        Object.assign(this.configMember, updated)
        this.configNotice = this.t('配置已保存；请新建 View 后重新生成拓扑以应用变更。')
      } catch (error) { this.configError = error.message || this.t('保存失败') } finally { this.configSaving = false }
    },
    scopeSelected(scope) { return scope.members.length > 0 && scope.members.every(member => this.selectedIds.includes(member.id)) },
    scopeIndeterminate(scope) { return !this.scopeSelected(scope) && scope.members.some(member => this.selectedIds.includes(member.id)) },
    toggleScope(scope, checked) {
      const ids = scope.members.map(member => member.id)
      this.selectedIds = checked ? [...new Set([...this.selectedIds, ...ids])] : this.selectedIds.filter(id => !ids.includes(id))
      if (checked) this.rememberMemberSnapshot(scope.members.find(member => member.current_snapshot_id))
    },
    runStatusText(status) { return this.t(({ pending: '等待中', running: '运行中', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' })[status] || status || '—') },
    attemptStatusText(status) { return this.t(({ pending: '等待中', running: '运行中', completed: '已完成', unchanged: '无变更', failed: '失败', cancelled: '已取消', interrupted: '已中断', queued: '已排队', already_running: '已有任务运行中' })[status] || status || '—') },
    stageText(stage) { return this.t(({ queued: '排队', validating: '校验', digest_before: '检查源码', codegraph_init: '初始化 CodeGraph', codegraph_sync: '同步 CodeGraph', digest_after: '复核源码', freezing_graph: '冻结图谱', freezing_sources: '冻结源码', digest_final: '最终校验', analyzing: '分析', publishing: '发布', finished: '完成' })[stage] || stage || '—') },
    attemptDetail(attempt) {
      if (!attempt) return '—'
      if (attempt.error_message) return attempt.error_message
      const progress = attempt.progress || {}
      return progress.message || progress.detail || (typeof progress.percent === 'number' ? `${progress.percent}%` : '—')
    },
    canCancelMember(member) { return this.isActiveRun && ['pending', 'running'].includes(member.attempt?.status) },
    async createView() {
      try {
        const response = await this.$api.request('/api/graph-views/current', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_ids: this.selectedMemberIds }) })
        this.view = response.view || response
        if (this.view?.id) {
          const selected = this.scopes.flatMap(scope => scope.members).find(member => this.selectedMemberIds.includes(member.id) && member.current_snapshot_id)
          rememberNavigationContext({
            viewId: this.view.id,
            snapshotId: selected?.current_snapshot_id,
            snapshotMember: selected?.display_name,
          })
        }
      } catch (error) { this.error = error }
    },
    async createRun() {
      this.runBusy = true; this.error = null
      try {
        const response = await this.$api.request('/api/analysis-runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_ids: this.selectedMemberIds }) })
        this.setActiveRun(response.run || response)
        this.schedulePoll()
      } catch (error) { this.error = error } finally { this.runBusy = false }
    },
    setActiveRun(run) {
      this.activeRun = run
      if (run?.id) window.sessionStorage?.setItem(LAST_RUN_KEY, run.id)
    },
    async loadRun(runId, { quiet = false } = {}) {
      try {
        const response = await this.$api.get(`/api/analysis-runs/${encodeURIComponent(runId)}`)
        this.setActiveRun(response.run || response)
        if (this.isActiveRun) this.schedulePoll()
        else { this.stopPolling(); await this.load() }
      } catch (error) {
        this.stopPolling()
        window.sessionStorage?.removeItem(LAST_RUN_KEY)
        if (!quiet) this.error = error
      }
    },
    schedulePoll() {
      this.stopPolling()
      if (this.isActiveRun) this.pollTimer = window.setTimeout(() => this.loadRun(this.activeRun.id, { quiet: true }), POLL_INTERVAL_MS)
    },
    stopPolling() { if (this.pollTimer) { window.clearTimeout(this.pollTimer); this.pollTimer = null } },
    async cancelRun() {
      this.runBusy = true
      try { const response = await this.$api.request(`/api/analysis-runs/${encodeURIComponent(this.activeRun.id)}/cancel`, { method: 'POST' }); this.setActiveRun(response.run || response); this.schedulePoll() } catch (error) { this.error = error } finally { this.runBusy = false }
    },
    async cancelMember(memberId) {
      this.runBusy = true
      try { const response = await this.$api.request(`/api/analysis-runs/${encodeURIComponent(this.activeRun.id)}/members/${encodeURIComponent(memberId)}/cancel`, { method: 'POST' }); this.setActiveRun(response.run || response); this.schedulePoll() } catch (error) { this.error = error } finally { this.runBusy = false }
    },
    async retryFailed() {
      this.runBusy = true; this.error = null
      try {
        const response = await this.$api.request(`/api/analysis-runs/${encodeURIComponent(this.activeRun.id)}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_ids: this.retryableMemberIds }) })
        this.setActiveRun(response.run || response)
        this.schedulePoll()
      } catch (error) { this.error = error } finally { this.runBusy = false }
    },
  },
}
</script>

<style scoped>
.snapshot-scope, .run-card { margin-bottom: 24px; }
.view-card, .run-card { padding: 12px; background: #f4f7fb; border-radius: 6px; }
.view-card { display: flex; gap: 12px; align-items: center; margin-bottom: 18px; }
.member-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.view-card small { color: #777; flex: 1; }
.run-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.run-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.run-card .muted { margin: 8px 0; }
.run-members { margin-top: 10px; }
.snapshot-scope h2 { margin: 0 0 10px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.page-header h1 { font-size: 24px; margin-bottom: 4px; }
.page-header p { color: #777; font-size: 13px; }
.actions { display: flex; gap: 8px; flex-shrink: 0; }
.primary { border: 0; background: #e94560; color: #fff; }
.secondary { border: 1px solid #cdd2dc; background: #fff; color: #444; }
.primary, .secondary { display: inline-flex; align-items: center; justify-content: center; text-decoration: none; }
.actions button, .member-actions .primary, .member-actions .secondary { border-radius: 6px; padding: 8px 13px; cursor: pointer; }
.member-actions .primary.sm, .member-actions .secondary.sm { padding: 6px 10px; font-size: 11px; }
button:disabled { opacity: .55; cursor: wait; }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px; border-bottom: 1px solid var(--border, #ddd); text-align: left; }
.attempt-detail { max-width: 320px; word-break: break-word; }
.run-status { margin-left: 8px; }
.status-pending, .status-queued { color: #8b5e00; }.status-running { color: #1769aa; }.status-completed, .status-unchanged { color: #19703a; }.status-failed, .status-interrupted { color: #b3261e; }.status-cancelled { color: #666; }
.muted { color: #777; }
.member-aliases { display: block; margin-top: 3px; color: #777; font-size: 11px; font-weight: normal; overflow-wrap: anywhere; }
.member-config-backdrop { position: fixed; inset: 0; z-index: 20; display: flex; align-items: center; justify-content: center; padding: 18px; background: rgb(15 23 42 / 55%); }
.member-config-dialog { width: min(560px, 100%); max-height: calc(100vh - 36px); overflow: auto; padding: 20px; background: #fff; border-radius: 10px; box-shadow: 0 18px 60px rgb(0 0 0 / 35%); }
.member-config-dialog header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.member-config-dialog h2 { margin: 0; font-size: 18px; }
.member-config-dialog label { display: block; margin-top: 14px; }
.member-config-dialog label span { display: block; margin-bottom: 6px; color: #333; font-size: 13px; font-weight: 600; }
.member-config-dialog input { width: 100%; box-sizing: border-box; padding: 9px 10px; border: 1px solid #cdd2dc; border-radius: 6px; font: inherit; }
.member-config-dialog label small { display: block; margin-top: 5px; color: #777; font-size: 11px; line-height: 1.45; }
.config-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.config-actions button { border-radius: 6px; padding: 8px 14px; cursor: pointer; }
.config-notice { margin: 12px 0 0; color: #19703a; font-size: 12px; }
.config-error { margin: 12px 0 0; color: #b3261e; font-size: 12px; }
@media (max-width: 720px) {
  .member-config-backdrop { padding: 0; }
  .member-config-dialog { width: 100vw; height: 100vh; height: 100dvh; max-height: none; border-radius: 0; }
  .page-header { display: block; margin-bottom: 14px; }
  .page-header h1 { font-size: 20px; }
  .page-header p { font-size: 12px; line-height: 1.5; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .actions button { flex: 1 1 calc(50% - 4px); min-height: 40px; }
  .snapshot-scope, .run-card { margin-bottom: 18px; }
  .view-card { flex-wrap: wrap; gap: 6px 10px; }
  .view-card small { flex-basis: 100%; overflow-wrap: anywhere; }
  .run-header { display: block; }
  .run-actions { margin-top: 8px; }
  .snapshot-scope h2 { font-size: 18px; overflow-wrap: anywhere; }
  .member-cards, .run-member-cards { display: grid; gap: 10px; }
  .member-select-all { display: flex; align-items: center; gap: 8px; padding: 2px 4px 4px; color: #555; font-size: 12px; }
  .member-card, .run-member-card { padding: 12px; border: 1px solid #e7e7eb; border-radius: 8px; background: #fff; }
  .member-card-head, .run-member-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
  .member-card-head label { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }
  .member-card-head b { font-size: 14px; overflow-wrap: anywhere; }
  .member-card-state { flex: none; padding: 2px 7px; border-radius: 10px; background: #eaf8f0; color: #23764a; font-size: 11px; }
  .member-card-state.unparsed { background: #f0f0f5; color: #666; }
  .member-card-snapshot { display: flex; align-items: baseline; gap: 8px; margin: 8px 0 0; color: #777; font-size: 11px; }
  .member-card-snapshot span { flex: none; }
  .member-card-snapshot code { color: #444; font-size: 11px; word-break: break-all; }
  .member-card-check { margin: 6px 0 0; font-size: 11px; }
  .member-card .member-actions { margin-top: 10px; }
  .member-card .member-actions .primary, .member-card .member-actions .secondary { flex: 1; min-height: 36px; padding: 6px 10px; font-size: 12px; }
  .run-member-head b { font-size: 14px; overflow-wrap: anywhere; }
  .run-member-card p { display: flex; gap: 8px; margin: 7px 0 0; color: #555; font-size: 12px; line-height: 1.45; }
  .run-member-card p > span { flex: none; color: #888; font-size: 11px; }
  .run-member-detail { overflow-wrap: anywhere; }
  .run-member-card button { margin-top: 9px; min-height: 34px; }
  .member-actions .primary.sm, .member-actions .secondary.sm { min-height: 32px; }
}
</style>
