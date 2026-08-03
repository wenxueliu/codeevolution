<template>
  <div class="home">
    <div v-if="error" class="request-error">{{ error.message }}</div>
    <div v-if="loading" class="request-loading">加载中...</div>
    <h1>代码仓列表</h1>

    <div class="repo-grid" v-if="repos.length">
      <div v-for="r in repos" :key="r.name" class="repo-card" @click="$router.push('/repo/' + r.name)">
        <div class="repo-header">
          <h2>{{ r.name }}</h2>
          <button class="remove-button" title="移除注册" @click.stop="removeRepo(r)">删除</button>
        </div>
        <div class="repo-path">{{ r.path }}</div>
        <div class="repo-members" v-if="r.repositories?.length > 1">
          <span v-for="member in r.repositories" :key="member.path">
            {{ member.name }}
          </span>
        </div>
        <div class="repo-stats" v-if="r.stats">
          <div class="stat"><b>{{ r.stats.total_commits }}</b> commits</div>
          <div class="stat"><b>{{ r.stats.active_features }}</b> 活跃功能</div>
          <div class="stat"><b>{{ r.stats.total_events }}</b> 事件</div>
        </div>
        <div class="repo-empty" v-else>未分析 — 运行 backfill 后可用</div>
      </div>
    </div>

    <div class="empty-state" v-else>
      <p>暂无已注册的代码仓</p>
      <code>codehistory register --name myproject --repo /path/to/repo</code>
      <p class="hint">注册后刷新页面即可看到</p>
    </div>
  </div>
</template>

<script>
export default {
  data() { return { repos: [] } },
  async created() { await this.loadRepos() },
  methods: {
    async loadRepos() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/repos')
        this.repos = data.repos || []
      })
    },
    async removeRepo(repo) {
      const confirmed = window.confirm(
        `确定从 CodeHistory 移除“${repo.name}”吗？\n\n仅删除注册记录，不会删除代码仓、CodeGraph 或演进数据库。`,
      )
      if (!confirmed) return
      await this.$runAsync(async () => {
        await this.$api.delete(`/api/repos/${encodeURIComponent(repo.name)}`)
        this.repos = this.repos.filter(item => item.name !== repo.name)
      })
    },
  },
}
</script>

<style scoped>
.home h1 { font-size: 24px; margin-bottom: 24px; }
.repo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.repo-card {
  background: #fff; border-radius: 8px; padding: 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08); cursor: pointer;
  transition: box-shadow 0.2s, transform 0.2s;
}
.repo-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); transform: translateY(-1px); }
.repo-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.repo-card h2 { font-size: 18px; color: #e94560; margin-bottom: 4px; }
.remove-button { border: 1px solid #e4b8bf; background: #fff; color: #b8324a; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; }
.remove-button:hover { color: #fff; background: #c8324d; border-color: #c8324d; }
.repo-path { font-size: 12px; color: #999; font-family: monospace; margin-bottom: 12px; word-break: break-all; }
.repo-members { display: flex; flex-wrap: wrap; gap: 5px; margin: -4px 0 12px; }
.repo-members span { padding: 2px 7px; border-radius: 10px; background: #fff0f2; color: #c82d48; font-size: 10px; }
.repo-stats { display: flex; gap: 20px; font-size: 13px; color: #666; }
.repo-stats b { color: #333; }
.repo-empty { font-size: 13px; color: #ffc107; }
.empty-state { text-align: center; padding: 60px 0; color: #888; }
.empty-state p { margin-bottom: 12px; font-size: 16px; }
.empty-state code { background: #f5f5f5; padding: 8px 16px; border-radius: 4px; font-size: 13px; display: inline-block; margin-bottom: 12px; }
.empty-state .hint { font-size: 13px; }
</style>
