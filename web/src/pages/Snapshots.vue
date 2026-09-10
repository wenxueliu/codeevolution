<template>
  <div class="snapshots-page">
    <div class="page-header">
      <div><h1>Snapshots</h1><p>选择仓库成员后运行分析；分析会自动初始化并同步 CodeGraph。</p></div>
      <div class="actions"><button class="secondary" :disabled="loading" @click="load">刷新</button><button class="secondary" :disabled="loading || runBusy || !selectedMemberIds.length" @click="createRun">运行分析</button><button class="primary" :disabled="loading || !selectedMemberIds.length" @click="createView">创建当前 View</button></div>
    </div>
    <UiState v-if="error" kind="error" title="Snapshot 加载失败" :message="error.message" action-label="重试" @action="load" />
    <UiState v-else-if="loading" kind="loading" title="正在加载 Snapshots" />
    <div v-else class="snapshot-scopes">
      <div v-if="view" class="view-card"><b>当前 View</b> <code>{{ view.id }}</code> <small>{{ view.digest }}</small><router-link :to="{ name: 'graph-view', params: { viewId: view.id } }">查看跨仓图谱</router-link><a :href="`/api/graph-views/${view.id}/export`" target="_blank">导出</a></div>

      <section v-if="activeRun" class="run-card" data-testid="analysis-run">
        <div class="run-header">
          <div><b>分析 Run</b> <code>{{ activeRun.id }}</code><span class="run-status" :class="`status-${activeRun.status}`">{{ runStatusText(activeRun.status) }}</span></div>
          <div class="run-actions">
            <button v-if="isActiveRun" class="secondary sm" :disabled="runBusy" @click="cancelRun">取消 Run</button>
            <button v-if="retryableMemberIds.length" class="secondary sm" :disabled="runBusy" @click="retryFailed">重试失败成员（{{ retryableMemberIds.length }}）</button>
          </div>
        </div>
        <p v-if="isActiveRun" class="muted">正在轮询成员进度；离开后重新打开本页可继续查看此 Run。</p>
        <p v-else-if="activeRun.status === 'partial'" class="muted">部分成员未完成；已发布的 Snapshot 保持可用。</p>
        <table class="run-members"><thead><tr><th>成员</th><th>状态</th><th>阶段</th><th>进度/错误</th><th></th></tr></thead>
          <tbody><tr v-for="member in activeRun.members || []" :key="member.member_id">
            <td>{{ memberName(member.member_id) }}</td>
            <td><span :class="`status-${member.attempt?.status || member.disposition}`">{{ attemptStatusText(member.attempt?.status || member.disposition) }}</span></td>
            <td>{{ stageText(member.attempt?.stage) }}</td>
            <td class="attempt-detail">{{ attemptDetail(member.attempt) }}</td>
            <td><button v-if="canCancelMember(member)" class="secondary sm" :disabled="runBusy" @click="cancelMember(member.member_id)">取消</button></td>
          </tr></tbody>
        </table>
      </section>

      <section v-for="scope in scopes" :key="scope.id" class="snapshot-scope">
        <h2>{{ scope.name }}</h2>
        <table><thead><tr><th><input :aria-label="`选择 ${scope.name} 全部成员`" type="checkbox" :checked="scopeSelected(scope)" :indeterminate.prop="scopeIndeterminate(scope)" @change="toggleScope(scope, $event.target.checked)"></th><th>成员</th><th>当前 Snapshot</th><th>状态</th><th></th></tr></thead>
          <tbody><tr v-for="member in scope.members" :key="member.id">
            <td><input v-model="selectedIds" type="checkbox" :value="member.id" :aria-label="`选择 ${member.display_name}`"></td>
            <td>{{ member.display_name }}</td>
            <td><code>{{ member.current_snapshot_id || '—' }}</code></td>
            <td>{{ member.current_snapshot_id ? '已发布' : '未解析' }}</td>
            <td><router-link v-if="member.current_snapshot_id" class="primary sm" :to="knowledgeLink(member.current_snapshot_id)">查看知识</router-link></td>
          </tr></tbody>
        </table>
      </section>
      <p v-if="!scopes.length" class="muted">暂无分析 Scope。</p>
    </div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'

const ACTIVE_RUN_STATUSES = ['pending', 'running']
const RETRYABLE_ATTEMPT_STATUSES = ['failed', 'cancelled', 'interrupted']
const POLL_INTERVAL_MS = 1000
const LAST_RUN_KEY = 'codeevolution:last-analysis-run-id'

export default {
  components: { UiState },
  data() { return { scopes: [], selectedIds: [], loading: false, error: null, view: null, activeRun: null, runBusy: false, pollTimer: null } },
  computed: {
    selectedMemberIds() { return this.selectedIds.filter(id => this.memberIds.includes(id)) },
    memberIds() { return this.scopes.flatMap(scope => scope.members.map(member => member.id)) },
    isActiveRun() { return ACTIVE_RUN_STATUSES.includes(this.activeRun?.status) },
    retryableMemberIds() { return (this.activeRun?.members || []).filter(member => RETRYABLE_ATTEMPT_STATUSES.includes(member.attempt?.status)).map(member => member.member_id) },
  },
  async created() {
    await this.load()
    const runId = window.sessionStorage?.getItem(LAST_RUN_KEY)
    if (runId) await this.loadRun(runId, { quiet: true })
  },
  beforeUnmount() { this.stopPolling() },
  methods: {
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
      } catch (error) { this.error = error } finally { this.loading = false }
    },
    knowledgeLink(snapshotId) { return { name: 'knowledge', params: { repoName: 'snapshot' }, query: { snapshot_id: snapshotId } } },
    memberName(memberId) { return this.scopes.flatMap(scope => scope.members).find(member => member.id === memberId)?.display_name || memberId },
    scopeSelected(scope) { return scope.members.length > 0 && scope.members.every(member => this.selectedIds.includes(member.id)) },
    scopeIndeterminate(scope) { return !this.scopeSelected(scope) && scope.members.some(member => this.selectedIds.includes(member.id)) },
    toggleScope(scope, checked) {
      const ids = scope.members.map(member => member.id)
      this.selectedIds = checked ? [...new Set([...this.selectedIds, ...ids])] : this.selectedIds.filter(id => !ids.includes(id))
    },
    runStatusText(status) { return ({ pending: '等待中', running: '运行中', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' })[status] || status || '—' },
    attemptStatusText(status) { return ({ pending: '等待中', running: '运行中', completed: '已完成', unchanged: '无变更', failed: '失败', cancelled: '已取消', interrupted: '已中断', queued: '已排队', already_running: '已有任务运行中' })[status] || status || '—' },
    stageText(stage) { return ({ queued: '排队', validating: '校验', digest_before: '检查源码', codegraph_init: '初始化 CodeGraph', codegraph_sync: '同步 CodeGraph', digest_after: '复核源码', freezing_graph: '冻结图谱', freezing_sources: '冻结源码', digest_final: '最终校验', analyzing: '分析', publishing: '发布', finished: '完成' })[stage] || stage || '—' },
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
.view-card small { color: #777; flex: 1; }
.run-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.run-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.run-card .muted { margin: 8px 0; }
.run-members { margin-top: 10px; }
.snapshot-scope h2 { margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px; border-bottom: 1px solid var(--border, #ddd); text-align: left; }
.attempt-detail { max-width: 320px; word-break: break-word; }
.run-status { margin-left: 8px; }
.status-pending, .status-queued { color: #8b5e00; }.status-running { color: #1769aa; }.status-completed, .status-unchanged { color: #19703a; }.status-failed, .status-interrupted { color: #b3261e; }.status-cancelled { color: #666; }
.muted { color: #777; }
</style>
