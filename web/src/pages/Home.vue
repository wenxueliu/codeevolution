<template>
  <div class="home">
    <UiState v-if="error" kind="error" title="代码仓加载失败" :message="error.message" action-label="重试" @action="loadRepos" />
    <UiState v-else-if="loading" kind="loading" title="正在加载代码仓" />
    <div class="page-header">
      <div><h1>代码仓列表</h1><p>查看索引与演进状态，进入仓库继续分析。</p></div>
      <button class="primary" type="button" @click="showRegister = !showRegister">{{ showRegister ? '取消添加' : '添加代码仓' }}</button>
    </div>

    <form v-if="showRegister" class="register-form" @submit.prevent="registerRepo">
      <label>服务名称<input v-model.trim="newRepo.name" required placeholder="例如：mall" /></label>
      <label>代码仓绝对路径<input v-model.trim="newRepo.path" required placeholder="例如：/workspace/mall" /></label>
      <button class="primary" :disabled="registering">{{ registering ? '正在注册...' : '注册代码仓' }}</button>
      <p>注册只保存路径，不会修改代码仓。多仓逻辑服务可继续使用 CLI 添加成员。</p>
    </form>

    <div class="repo-grid" v-if="repos.length">
      <article v-for="r in repos" :key="r.name" class="repo-card">
        <router-link class="repo-link" :to="'/repo/' + r.name" :aria-label="`进入代码仓 ${r.name}`">
          <div class="repo-header"><h2>{{ r.name }}</h2><span aria-hidden="true">→</span></div>
          <div class="repo-path">{{ r.path }}</div>
          <div class="repo-members">
            <span v-for="member in r.repositories || []" :key="member.path" :class="{ unhealthy: !member.cg_initialized }">
              {{ member.name }} · {{ member.cg_initialized ? '索引就绪' : '未初始化索引' }}
            </span>
          </div>
          <div class="repo-stats" v-if="hasAnalysis(r)">
            <div class="stat"><b>{{ r.stats.total_commits }}</b> 已分析提交</div>
            <div class="stat"><b>{{ r.stats.active_features }}</b> 活跃功能</div>
            <div class="stat"><b>{{ r.stats.total_events }}</b> 演变事件</div>
          </div>
          <div class="repo-empty" v-else>
            <strong>尚未生成演进数据</strong>
            <span>运行首次回溯后即可查看功能和事件。</span>
            <code>codehistory backfill -r {{ r.path }}</code>
          </div>
        </router-link>
        <button class="remove-button" type="button" title="移除注册" @click="removeRepo(r)">删除</button>
      </article>
    </div>

    <div class="empty-state" v-else>
      <UiState title="还没有代码仓" message="添加一个已初始化 CodeGraph 的本地仓库，开始提取结构知识和演进数据。" action-label="添加代码仓" @action="showRegister = true" />
    </div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'

export default {
  components: { UiState },
  data() { return { repos: [], showRegister: false, registering: false, newRepo: { name: '', path: '' } } },
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
    async registerRepo() {
      this.registering = true
      await this.$runAsync(async () => {
        await this.$api.request('/api/repos/register', { method: 'POST', query: { name: this.newRepo.name, path: this.newRepo.path } })
        this.newRepo = { name: '', path: '' }
        this.showRegister = false
        await this.loadRepos()
      })
      this.registering = false
    },
    hasAnalysis(repo) { return Boolean(repo.stats && repo.stats.total_commits > 0) },
  },
}
</script>

<style scoped>
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.home h1 { font-size: 24px; margin-bottom: 4px; }
.page-header p, .register-form p { color: #777; font-size: 13px; }
.primary { border: 0; border-radius: 6px; padding: 9px 14px; background: #e94560; color: #fff; cursor: pointer; }
.register-form { display: grid; grid-template-columns: 220px minmax(280px, 1fr) auto; align-items: end; gap: 12px; padding: 16px; margin-bottom: 18px; background: white; border: 1px solid #e7e7eb; border-radius: 8px; }
.register-form label { display: grid; gap: 5px; color: #555; font-size: 12px; }
.register-form input { border: 1px solid #cfd3da; border-radius: 5px; padding: 8px 9px; }
.register-form p { grid-column: 1 / -1; margin: 0; }
.repo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.repo-card {
  position: relative; background: #fff; border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  transition: box-shadow 0.2s, transform 0.2s;
}
.repo-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); transform: translateY(-1px); }
.repo-link { display: block; padding: 20px; color: inherit; text-decoration: none; }
.repo-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.repo-card h2 { font-size: 18px; color: #e94560; margin-bottom: 4px; }
.remove-button { position: absolute; right: 16px; top: 16px; border: 1px solid #e4b8bf; background: #fff; color: #b8324a; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; }
.repo-header > span { margin-right: 56px; color: #a6acb6; }
.remove-button:hover { color: #fff; background: #c8324d; border-color: #c8324d; }
.repo-path { font-size: 12px; color: #999; font-family: monospace; margin-bottom: 12px; word-break: break-all; }
.repo-members { display: flex; flex-wrap: wrap; gap: 5px; margin: -4px 0 12px; }
.repo-members span { padding: 2px 7px; border-radius: 10px; background: #eaf8f0; color: #23764a; font-size: 10px; }
.repo-members span.unhealthy { background: #fff3db; color: #8a6418; }
.repo-stats { display: flex; gap: 20px; font-size: 13px; color: #666; }
.repo-stats b { color: #333; }
.repo-empty { display: grid; gap: 5px; font-size: 12px; color: #73622d; background: #fff9e8; padding: 10px; border-radius: 6px; }
.repo-empty code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #66561f; }
.empty-state { text-align: center; padding: 60px 0; color: #888; }
.empty-state p { margin-bottom: 12px; font-size: 16px; }
.empty-state code { background: #f5f5f5; padding: 8px 16px; border-radius: 4px; font-size: 13px; display: inline-block; margin-bottom: 12px; }
.empty-state .hint { font-size: 13px; }
@media (max-width: 720px) {
  .page-header { align-items: center; }
  .register-form { grid-template-columns: 1fr; }
  .register-form p { grid-column: auto; }
  .repo-grid { grid-template-columns: 1fr; }
  .repo-stats { gap: 12px; flex-wrap: wrap; }
}
</style>
