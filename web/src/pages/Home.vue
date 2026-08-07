<template>
  <div class="home">
    <UiState v-if="error" kind="error" title="代码仓加载失败" :message="error.message" action-label="重试" dismiss-label="关闭" @action="loadRepos" @dismiss="error = null" />
    <UiState v-else-if="loading" kind="loading" title="正在加载代码仓" />
    <div class="page-header">
      <div><h1>代码仓列表</h1><p>查看索引与演进状态，进入仓库继续分析。</p></div>
      <button class="primary" type="button" @click="showRegister = !showRegister">{{ showRegister ? '取消添加' : '添加代码仓' }}</button>
    </div>

    <form v-if="showRegister" class="register-form" @submit.prevent="registerRepo">
      <label>服务名称<input v-model.trim="newRepo.name" required placeholder="例如：mall" /></label>
      <label>代码仓绝对路径<input v-model.trim="newRepo.path" required placeholder="例如：/workspace/mall" /></label>
      <button class="primary" :disabled="registering">{{ registering ? '正在注册...' : '注册代码仓' }}</button>
      <p>注册只保存路径，不会修改代码仓。注册后可在卡片中继续添加同服务的其他代码仓。</p>
    </form>

    <div class="repo-grid" v-if="repos.length">
      <article v-for="r in repos" :key="r.name" class="repo-card">
        <router-link class="repo-link" :to="'/repo/' + r.name" :aria-label="`进入代码仓 ${r.name}`">
          <div class="repo-header"><h2>{{ r.name }}</h2><span aria-hidden="true">→</span></div>
          <div class="repo-path">{{ r.path }}</div>
          <div class="repo-members">
            <span v-for="member in r.repositories || []" :key="member.path" :class="{ unhealthy: !member.cg_initialized }">
              <code class="member-path">{{ member.path }}</code>
              {{ member.cg_initialized ? '索引就绪' : '未初始化索引' }}
              <button class="member-remove" type="button" title="移除此代码仓" @click.prevent.stop="removeMember(r, member.path)">x</button>
            </span>
            <span v-if="!(r.repositories && r.repositories.length)" class="single-repo">
              <code class="member-path">{{ r.path }}</code> · 单仓服务
            </span>
          </div>
          <div class="member-add" v-if="addMemberForm[r.name] !== undefined">
            <input v-model.trim="addMemberPath[r.name]" placeholder="代码仓绝对路径" @keyup.enter="addMember(r)" />
            <button class="primary sm" :disabled="addingMember[r.name]" @click.stop="addMember(r)">{{ addingMember[r.name] ? '添加中...' : '确认' }}</button>
            <button class="secondary sm" @click.stop="toggleAddMember(r)">取消</button>
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
        <div class="repo-actions">
          <button class="add-member-button" type="button" title="添加代码仓" @click.stop="toggleAddMember(r)">+ 代码仓</button>
          <button
            class="init-button"
            type="button"
            :disabled="initState(r).busy"
            :title="initState(r).title"
            @click="initRepo(r)"
          >
            <span v-if="initState(r).busy" class="spinner"></span>
            {{ initState(r).label }}
          </button>
          <button class="remove-button" type="button" title="移除注册" @click="removeRepo(r)">删除</button>
        </div>
        <div class="init-progress" v-if="initState(r).detail">
          <div class="progress-bar-wrap" v-if="initState(r).total > 0">
            <div class="progress-bar-fill" :style="{ width: initState(r).pct + '%' }" :class="'bar-' + initState(r).taskStatus"></div>
            <span class="progress-bar-text">{{ initState(r).done }}/{{ initState(r).total }} ({{ initState(r).pct }}%)</span>
          </div>
          <div class="progress-steps">
            <span
              v-for="(step, si) in initState(r).steps"
              :key="si"
              :class="'step-' + step.status"
            >{{ step.member }}: {{ step.step === 'codegraph_init' ? '索引' : '回溯' }}
              {{ step.status === 'completed' ? '✓' : step.status === 'running' ? '...' : '✗' }}</span>
          </div>
          <div class="progress-error" v-if="initState(r).error">{{ initState(r).error }}</div>
        </div>
      </article>
    </div>

    <div class="empty-state" v-else>
      <UiState title="还没有代码仓" message="添加一个已初始化 CodeGraph 的本地仓库，开始提取结构知识和演进数据。" action-label="添加代码仓" @action="showRegister = true" />
    </div>
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'

const INIT_POLL_MS = 2000

export default {
  components: { UiState },
  data() {
    return {
      repos: [],
      showRegister: false,
      registering: false,
      newRepo: { name: '', path: '' },
      initTasks: {},
      addMemberForm: {},
      addMemberPath: {},
      addingMember: {},
      _pollTimer: null,
    }
  },
  async created() {
    await this.loadRepos()
    this._pollTimer = setInterval(() => this._pollInitTasks(), INIT_POLL_MS)
  },
  beforeUnmount() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null }
  },
  methods: {
    async loadRepos() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/repos')
        this.repos = data.repos || []
      })
    },
    async removeRepo(repo) {
      const confirmed = window.confirm(
        `确定从 CodeHistory 移除”${repo.name}”吗？\n\n仅删除注册记录，不会删除代码仓、CodeGraph 或演进数据库。`,
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
        await this.$api.request(`/api/repos/${encodeURIComponent(repo.name)}/members`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: p }),
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

    async removeMember(repo, memberPath) {
      const confirmed = window.confirm(`确定从"${repo.name}"中移除此代码仓吗？\n\n${memberPath}\n\n仅删除注册记录，不会删除实际代码仓数据。`)
      if (!confirmed) return
      try {
        await this.$api.delete(`/api/repos/${encodeURIComponent(repo.name)}/members?path=${encodeURIComponent(memberPath)}`)
        await this.loadRepos()
      } catch (err) {
        this.error = err
      }
    },

    // ── one-click init ──

    async initRepo(repo) {
      const task = this.initTasks[repo.name]
      if (task && (task.status === 'pending' || task.status === 'running')) return
      this.initTasks[repo.name] = { status: 'pending', progress: [], service: repo.name }
      try {
        await this.$api.request(`/api/repos/${encodeURIComponent(repo.name)}/init`, { method: 'POST' })
        await this._fetchInitStatus(repo.name)
      } catch (err) {
        this.initTasks[repo.name] = { ...this.initTasks[repo.name], status: 'failed', error: (err.body && (err.body.detail || err.body.message)) || err.message || '请求失败' }
      }
    },

    initState(repo) {
      const task = this.initTasks[repo.name]
      if (!task) return { busy: false, label: '一键初始化', title: '初始化索引并回溯全量历史', detail: false, steps: [], error: '', total: 0, done: 0, pct: 0, taskStatus: '' }

      const isBusy = task.status === 'pending' || task.status === 'running'
      const steps = task.progress || []
      const total = task.total || 1
      // Count completed *repos* (a repo is done when all its steps are completed)
      const memberSteps = {}
      for (const s of steps) {
        if (!memberSteps[s.member]) memberSteps[s.member] = []
        memberSteps[s.member].push(s)
      }
      const doneRepos = Object.values(memberSteps).filter(ss => ss.every(s => s.status === 'completed')).length
      const done = doneRepos
      const pct = Math.round((done / Math.max(total, 1)) * 100)

      let label = '一键初始化'
      let title = '初始化索引并回溯全量历史'
      if (isBusy) {
        label = `初始化中 ${done}/${total}`
        title = '正在初始化...'
      } else if (task.status === 'completed') {
        label = '初始化完成 ✓'
        title = '所有成员初始化成功'
      } else if (task.status === 'partial') {
        label = '部分完成 ⚠'
        title = '部分成员初始化失败'
      } else if (task.status === 'failed') {
        label = '初始化失败 ✗'
        title = task.error || '初始化失败'
      }

      return { busy: isBusy, label, title, detail: !!task.status, steps, error: task.error || '', total, done, pct, taskStatus: task.status || '' }
    },

    async _fetchInitStatus(name) {
      try {
        const task = await this.$api.get(`/api/repos/${encodeURIComponent(name)}/init/status`)
        this.initTasks[name] = task
        if (task.status === 'completed' || task.status === 'partial') {
          await this.loadRepos()
        }
      } catch {
        // task not found — ignore
      }
    },

    async _pollInitTasks() {
      const running = Object.entries(this.initTasks).filter(
        ([, t]) => t.status === 'pending' || t.status === 'running',
      )
      for (const [name] of running) {
        await this._fetchInitStatus(name)
      }
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
.init-button:hover:not(:disabled) { color: #fff; background: #2a6496; border-color: #2a6496; }
.init-button:disabled { opacity: 0.7; cursor: not-allowed; }
.remove-button { border: 1px solid #e4b8bf; background: #fff; color: #b8324a; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; }
.repo-header > span { margin-right: 120px; color: #a6acb6; }
.remove-button:hover { color: #fff; background: #c8324d; border-color: #c8324d; }
.spinner { width: 10px; height: 10px; border: 2px solid #cfd3da; border-top-color: #2a6496; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.init-progress { padding: 0 20px 14px; }
.progress-bar-wrap { position: relative; height: 22px; background: #ececf2; border-radius: 11px; overflow: hidden; margin-bottom: 10px; }
.progress-bar-fill { height: 100%; border-radius: 11px; transition: width 0.5s ease; min-width: 2px; }
.bar-running, .bar-pending { background: linear-gradient(90deg, #4a90d9, #6cb3f5); }
.bar-completed { background: linear-gradient(90deg, #3cba7a, #5dd99a); }
.bar-partial { background: linear-gradient(90deg, #f0a030, #f5c060); }
.bar-failed { background: linear-gradient(90deg, #d94a4a, #e87070); }
.progress-bar-text { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); font-size: 10px; font-weight: 600; color: #333; white-space: nowrap; }
.progress-steps { display: flex; flex-wrap: wrap; gap: 4px; }
.progress-steps span { padding: 1px 6px; border-radius: 8px; font-size: 10px; }
.progress-steps .step-running { background: #e3f0fc; color: #2a6496; }
.progress-steps .step-completed { background: #eaf8f0; color: #23764a; }
.progress-steps .step-failed { background: #ffeaea; color: #b8324a; }
.progress-error { margin-top: 6px; font-size: 11px; color: #b8324a; }
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
