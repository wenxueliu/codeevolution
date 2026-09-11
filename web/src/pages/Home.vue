<template>
  <div class="home">
    <UiState v-if="error" kind="error" title="代码仓加载失败" :message="error.message" action-label="重试" dismiss-label="关闭" @action="loadRepos" @dismiss="error = null" />
    <UiState v-else-if="loading" kind="loading" title="正在加载代码仓" />
    <div class="page-header">
      <div><h1>代码仓列表</h1><p>查看代码仓索引状态，进入仓库继续分析。</p></div>
      <button data-testid="add-repository" class="primary" type="button" @click="showRegister = !showRegister">{{ showRegister ? '取消添加' : '添加代码仓' }}</button>
    </div>

    <form v-if="showRegister" data-testid="register-form" class="register-form" @submit.prevent="registerRepo">
      <label>服务名称<input data-testid="repository-name" v-model.trim="newRepo.name" required placeholder="例如：mall" /></label>
      <label>代码仓绝对路径<input data-testid="repository-path" v-model.trim="newRepo.path" required placeholder="例如：/workspace/mall" /></label>
      <button data-testid="register-repository" class="primary" :disabled="registering">{{ registering ? '正在注册...' : '注册代码仓' }}</button>
      <p>注册只保存路径，不会修改代码仓。注册后可在卡片中继续添加同服务的其他代码仓。</p>
    </form>

    <div class="repo-grid" v-if="repos.length">
      <article v-for="r in repos" :key="r.name" class="repo-card">
        <router-link class="repo-link" :to="{ name: 'snapshots' }" :aria-label="`查看代码仓 ${r.name} 的 Snapshots`">
          <div class="repo-header"><h2>{{ r.name }}</h2><span aria-hidden="true">→</span></div>
          <div class="repo-path">{{ r.path }}</div>
          <div class="repo-members">
            <span v-for="member in r.repositories || []" :key="member.id || member.path" :class="{ unhealthy: !member.cg_initialized }">
              <code class="member-path">{{ member.path }}</code>
              {{ member.cg_initialized ? '索引就绪' : '未初始化索引' }}
              <button class="member-remove" type="button" title="移除此代码仓" @click.prevent.stop="removeMember(r, member)">x</button>
            </span>
            <span v-if="!(r.repositories && r.repositories.length)" class="single-repo">
              <code class="member-path">{{ r.path }}</code> · 单仓服务
            </span>
          </div>
          <div class="repo-enter">查看 Snapshots</div>
        </router-link>
        <div class="member-add" v-if="addMemberForm[r.name] !== undefined" @click.stop>
          <input data-testid="member-repository-path" v-model.trim="addMemberPath[r.name]" placeholder="代码仓绝对路径" @keyup.enter="addMember(r)" />
          <button data-testid="confirm-member-repository" class="primary sm" :disabled="addingMember[r.name]" @click.stop="addMember(r)">{{ addingMember[r.name] ? '添加中...' : '确认' }}</button>
          <button class="secondary sm" @click.stop="toggleAddMember(r)">取消</button>
        </div>
        <div class="repo-actions">
          <button data-testid="add-member" class="add-member-button" type="button" title="添加代码仓" @click.stop="toggleAddMember(r)">+ 代码仓</button>
          <button class="init-button" type="button" title="在 Snapshots 页面创建分析 Run" @click="openSnapshots">
            运行分析
          </button>
          <button class="remove-button" type="button" title="移除注册" @click="removeRepo(r)">删除</button>
        </div>
      </article>
    </div>

    <div class="empty-state" v-else>
      <UiState title="还没有代码仓" message="添加一个已初始化 CodeGraph 的本地仓库，开始提取结构知识。" action-label="添加代码仓" @action="showRegister = true" />
    </div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'

export default {
  components: { UiState },
  data() {
    return {
      repos: [],
      showRegister: false,
      registering: false,
      newRepo: { name: '', path: '' },
      addMemberForm: {},
      addMemberPath: {},
      addingMember: {},
    }
  },
  async created() {
    await this.loadRepos()
  },
  methods: {
    async loadRepos() {
      await this.$runAsync(async () => {
        // Home and Snapshots must use the same Scope/Member catalog. The
        // legacy /api/repos registry can contain a different set of entries.
        const data = await this.$api.get('/api/scopes')
        const scopes = []
        for (const scope of data.scopes || []) {
          const members = await this.$api.get(`/api/scopes/${encodeURIComponent(scope.id)}/members`)
          const repositories = (members.members || []).map(member => ({
            id: member.id,
            name: member.display_name,
            path: member.registered_path,
            cg_initialized: true,
          }))
          scopes.push({ id: scope.id, name: scope.name, path: repositories[0]?.path || '', repositories })
        }
        this.repos = scopes
      })
    },
    async removeRepo(repo) {
      const confirmed = window.confirm(
        `确定从 CodeEvolution 移除”${repo.name}”吗？\n\n仅删除注册记录，不会删除代码仓或 CodeGraph 数据。`,
      )
      if (!confirmed) return
      await this.$runAsync(async () => {
        await this.$api.delete(`/api/scopes/${encodeURIComponent(repo.id)}`)
        this.repos = this.repos.filter(item => item.name !== repo.name)
      })
    },
    async registerRepo() {
      this.registering = true
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/scopes')
        let scope = (data.scopes || []).find(item => item.name === this.newRepo.name)
        if (!scope) {
          const response = await this.$api.request('/api/scopes', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: this.newRepo.name }),
          })
          scope = response.scope || response
        }
        await this.$api.request(`/api/scopes/${encodeURIComponent(scope.id)}/members`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            display_name: this.newRepo.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || this.newRepo.name,
            registered_path: this.newRepo.path,
          }),
        })
        this.newRepo = { name: '', path: '' }
        this.showRegister = false
        await this.loadRepos()
      })
      this.registering = false
    },
    // ── member management ──

    toggleAddMember(repo) {
      if (this.addMemberForm[repo.name] !== undefined) {
        delete this.addMemberForm[repo.name]
        delete this.addMemberPath[repo.name]
      } else {
        this.addMemberForm[repo.name] = true
        this.addMemberPath[repo.name] = ''
      }
    },

    async addMember(repo) {
      const p = (this.addMemberPath[repo.name] || '').trim()
      if (!p) return
      this.addingMember[repo.name] = true
      try {
        await this.$api.request(`/api/scopes/${encodeURIComponent(repo.id)}/members`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            display_name: p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p,
            registered_path: p,
          }),
        })
        delete this.addMemberForm[repo.name]
        delete this.addMemberPath[repo.name]
        await this.loadRepos()
      } catch (err) {
        this.error = err
      } finally {
        delete this.addingMember[repo.name]
      }
    },

    async removeMember(repo, member) {
      const confirmed = window.confirm(`确定从"${repo.name}"中移除此代码仓吗？\n\n${member.path}\n\n仅删除目录成员，不会删除实际代码仓数据。`)
      if (!confirmed) return
      try {
        await this.$api.delete(`/api/repository-members/${encodeURIComponent(member.id)}`)
        await this.loadRepos()
      } catch (err) {
        this.error = err
      }
    },

    openSnapshots() {
      this.$router.push({ name: 'snapshots' })
    },
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
.repo-actions { display: flex; gap: 6px; position: absolute; right: 16px; top: 16px; }
.init-button { display: inline-flex; align-items: center; gap: 5px; border: 1px solid #a6c8e4; background: #fff; color: #2a6496; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; white-space: nowrap; }
.init-button:hover { color: #fff; background: #2a6496; border-color: #2a6496; }
.remove-button { border: 1px solid #e4b8bf; background: #fff; color: #b8324a; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; }
.repo-header > span { margin-right: 120px; color: #a6acb6; }
.remove-button:hover { color: #fff; background: #c8324d; border-color: #c8324d; }
.repo-path { font-size: 12px; color: #999; font-family: monospace; margin-bottom: 12px; word-break: break-all; }
.repo-members { display: flex; flex-direction: column; gap: 2px; margin: -4px 0 12px; }
.repo-members span { display: flex; align-items: center; gap: 5px; padding: 2px 7px; border-radius: 10px; background: #eaf8f0; color: #23764a; font-size: 10px; }
.repo-members span.unhealthy { background: #fff3db; color: #8a6418; }
.repo-members span.single-repo { background: #f0f0f5; color: #666; }
.member-path { font-size: 9px; color: #555; background: rgba(0,0,0,.05); padding: 1px 4px; border-radius: 3px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.member-remove { border: 0; background: none; color: #c8324d; cursor: pointer; font-size: 10px; font-weight: 700; padding: 0 2px; line-height: 1; margin-left: auto; opacity: 0.5; }
.member-remove:hover { opacity: 1; }
.member-add { display: flex; gap: 6px; padding: 0 20px 12px; }
.member-add input { flex: 1; border: 1px solid #cfd3da; border-radius: 5px; padding: 5px 8px; font-size: 11px; min-width: 0; }
.add-member-button { border: 1px solid #c5d8c5; background: #fff; color: #3a7d44; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; white-space: nowrap; }
.add-member-button:hover { color: #fff; background: #3a7d44; border-color: #3a7d44; }
.repo-enter { font-size: 13px; color: #e94560; font-weight: 600; }
.empty-state { text-align: center; padding: 60px 0; color: #888; }
.empty-state p { margin-bottom: 12px; font-size: 16px; }
.empty-state code { background: #f5f5f5; padding: 8px 16px; border-radius: 4px; font-size: 13px; display: inline-block; margin-bottom: 12px; }
.empty-state .hint { font-size: 13px; }
@media (max-width: 720px) {
  .page-header { align-items: center; }
  .register-form { grid-template-columns: 1fr; }
  .register-form p { grid-column: auto; }
  .repo-grid { grid-template-columns: 1fr; }
}
</style>
