<template>
  <div class="snapshots-page">
    <div class="page-header">
      <div><h1>Snapshots</h1><p>浏览不可变分析结果；进入知识页不会读取现场代码仓。</p></div>
      <div class="actions"><button class="secondary" :disabled="loading" @click="load">刷新</button><button class="secondary" :disabled="loading || !memberIds.length" @click="createRun">运行分析</button><button class="primary" :disabled="loading || !memberIds.length" @click="createView">创建当前 View</button></div>
    </div>
    <UiState v-if="error" kind="error" title="Snapshot 加载失败" :message="error.message" action-label="重试" @action="load" />
    <UiState v-else-if="loading" kind="loading" title="正在加载 Snapshots" />
    <div v-else class="snapshot-scopes">
      <div v-if="view" class="view-card"><b>当前 View</b> <code>{{ view.id }}</code> <small>{{ view.digest }}</small><a :href="`/api/graph-views/${view.id}/export`" target="_blank">导出</a></div>
      <section v-for="scope in scopes" :key="scope.id" class="snapshot-scope">
        <h2>{{ scope.name }}</h2>
        <table><thead><tr><th>成员</th><th>当前 Snapshot</th><th>状态</th><th></th></tr></thead>
          <tbody><tr v-for="member in scope.members" :key="member.id">
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

export default {
  components: { UiState },
  data() { return { scopes: [], loading: false, error: null, view: null } },
  computed: { memberIds() { return this.scopes.flatMap(scope => scope.members.map(member => member.id)) } },
  async created() { await this.load() },
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
      } catch (error) { this.error = error } finally { this.loading = false }
    },
    knowledgeLink(snapshotId) { return { name: 'knowledge', params: { repoName: 'snapshot' }, query: { snapshot_id: snapshotId } } },
    async createView() {
      try {
        const response = await this.$api.request('/api/graph-views/current', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_ids: this.memberIds }) })
        this.view = response.view || response
      } catch (error) { this.error = error }
    },
    async createRun() {
      try {
        await this.$api.request('/api/analysis-runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ member_ids: this.memberIds }) })
        await this.load()
      } catch (error) { this.error = error }
    },
  },
}
</script>

<style scoped>
.snapshot-scope { margin-bottom: 24px; }
.view-card { display: flex; gap: 12px; align-items: center; margin-bottom: 18px; padding: 12px; background: #f4f7fb; border-radius: 6px; }
.view-card small { color: #777; flex: 1; }
.snapshot-scope h2 { margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px; border-bottom: 1px solid var(--border, #ddd); text-align: left; }
.muted { color: #777; }
</style>
