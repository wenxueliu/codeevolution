<template>
  <div class="refactoring-page">
    <div v-if="error" class="request-error">{{ error.message }}</div>
    <div v-if="loading" class="request-loading">正在结合 Git 历史与 CodeGraph 分析热点代码...</div>

    <header class="page-header">
      <div>
        <h1>渐进式重构</h1>
        <p>一次只检查一种重构手法；缺少测试安全网时，只生成测试任务。</p>
      </div>
      <div class="header-actions">
        <button class="secondary" @click="beginCreate">新增手法</button>
        <button class="primary" :disabled="loading || !technique" @click="loadPlans">分析重构机会</button>
      </div>
    </header>

    <form v-if="editingTechnique" class="technique-form" @submit.prevent="saveTechnique">
      <div class="form-heading">
        <div><h2>{{ editingExisting ? '编辑重构手法' : '新增重构手法' }}</h2><p>检查项每行一条，用于约束 Agent 本轮只检查这些信号。</p></div>
        <button type="button" class="link" @click="cancelEdit">关闭</button>
      </div>
      <div class="form-grid">
        <label>唯一 ID<input v-model.trim="techniqueForm.id" :disabled="editingExisting" required pattern="[a-z][a-z0-9]*(-[a-z0-9]+)*" placeholder="extract-domain-service"></label>
        <label>名称<input v-model.trim="techniqueForm.name" required maxlength="100" placeholder="提取领域服务"></label>
        <label class="wide">目标<textarea v-model.trim="techniqueForm.objective" required maxlength="500" rows="2" placeholder="说明这项重构要达到的单一目标"></textarea></label>
        <label class="wide">检查项<textarea v-model="techniqueForm.checksText" required rows="4" placeholder="职责混合&#10;跨聚合调用&#10;领域逻辑散落"></textarea></label>
      </div>
      <div v-if="techniqueError" class="form-error">{{ techniqueError }}</div>
      <div class="form-actions"><button type="button" class="secondary" @click="cancelEdit">取消</button><button class="primary" :disabled="savingTechnique">{{ savingTechnique ? '保存中...' : '保存手法' }}</button></div>
    </form>

    <section class="filters">
      <label>代码仓
        <select v-model="repositoryMember" @change="onMemberChange">
          <option v-for="item in repositoryMembers" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label>重构手法
        <select v-model="technique">
          <option v-for="item in techniques" :key="item.id" :value="item.id">{{ item.name }} · {{ item.id }}</option>
        </select>
      </label>
      <label>窗口
        <select v-model.number="windowDays">
          <option :value="7">最近 1 周</option><option :value="14">最近 2 周</option>
          <option :value="28">最近 4 周</option><option :value="56">最近 8 周</option>
          <option :value="84">最近 12 周</option><option :value="168">最近 24 周</option>
        </select>
      </label>
      <label>已处理窗口
        <select v-model.number="previousWindowDays">
          <option :value="0">无（初始窗口）</option>
          <option v-for="days in previousWindowOptions" :key="days" :value="days">最近 {{ days }} 天</option>
        </select>
      </label>
      <label>最多候选
        <input v-model.number="limit" type="number" min="1" max="50">
      </label>
      <label>最少直接测试
        <input v-model.number="minTests" type="number" min="1" max="20">
      </label>
    </section>

    <section v-if="selectedTechnique" class="technique-summary">
      <div><b>{{ selectedTechnique.name }}</b><span>{{ selectedTechnique.objective }}</span></div>
      <div class="technique-tools">
        <div class="chips"><span v-for="check in selectedTechnique.checks" :key="check">{{ check }}</span></div>
        <button class="edit-button" @click="beginEdit(selectedTechnique)">编辑</button>
      </div>
    </section>

    <section class="summary-grid" v-if="plans.length">
      <div><strong>{{ plans.length }}</strong><span>重构候选</span></div>
      <div><strong>{{ refactorableCount }}</strong><span>允许重构</span></div>
      <div><strong>{{ protectedCount }}</strong><span>测试防护充分</span></div>
      <div><strong>{{ testFirstCount }}</strong><span>需要先补测试</span></div>
    </section>

    <div class="plan-list" v-if="plans.length">
      <article class="plan-card" v-for="(plan, index) in plans" :key="plan.hotspot.node_id + plan.repository_member">
        <button class="plan-heading" @click="toggle(index)">
          <span class="sequence">{{ index + 1 }}</span>
          <span class="heading-main">
            <b>{{ plan.hotspot.qualified_name }}</b>
            <small>{{ plan.repository_member }} · {{ location(plan) }}</small>
          </span>
          <span class="badge" :class="plan.test_gate.refactoring_allowed ? 'ready' : 'blocked'">
            {{ plan.test_gate.refactoring_allowed ? '可重构' : '测试优先' }}
          </span>
          <span class="risk" :class="plan.codegraph_impact.risk.toLowerCase()">{{ plan.codegraph_impact.risk }}</span>
          <span class="toggle">{{ expanded[index] ? '收起' : '详情' }}</span>
        </button>

        <div class="plan-body" v-if="expanded[index]">
          <div class="evidence-grid">
            <div><span>热点分</span><strong>{{ plan.hotspot.score }}</strong></div>
            <div><span>近期提交</span><strong>{{ plan.hotspot.commit_count }}</strong></div>
            <div><span>修改行</span><strong>{{ plan.hotspot.changed_lines }}</strong></div>
            <div><span>影响符号</span><strong>{{ plan.codegraph_impact.affected_symbol_count }}</strong></div>
          </div>

          <div class="detail-grid">
            <section>
              <h3>代码修改范围</h3>
              <code class="location">{{ location(plan) }}</code>
              <p class="scope-note">只允许围绕该热点形成最小修改闭包，不向二、三阶依赖扩散。</p>
              <h4>直接调用者</h4>
              <ul><li v-for="item in plan.codegraph_impact.direct_callers" :key="item.node_id"><b>{{ item.name }}</b><code>{{ item.file_path }}:{{ item.line }}</code></li></ul>
              <p class="empty" v-if="!plan.codegraph_impact.direct_callers.length">无直接调用者</p>
              <h4>直接依赖</h4>
              <ul><li v-for="(item, itemIndex) in plan.codegraph_impact.direct_callees" :key="item.target_node_id + itemIndex"><b>{{ item.name }}</b><code>{{ item.file_path }}:{{ item.line }}</code></li></ul>
              <p class="empty" v-if="!plan.codegraph_impact.direct_callees.length">无直接依赖</p>
            </section>

            <section>
              <h3>测试防护</h3>
              <div class="gate" :class="plan.test_gate.status">
                <b>{{ gateLabel(plan.test_gate.status) }}</b>
                <span>{{ plan.test_gate.assessment }}</span>
              </div>
              <ul><li v-for="test in plan.test_gate.related_tests" :key="test.qualified_name"><b>{{ test.qualified_name }}</b><code>{{ test.file_path }}:{{ test.line }} · {{ test.coverage }}</code></li></ul>
              <p class="empty" v-if="!plan.test_gate.related_tests.length">CodeGraph 未发现关联测试。</p>
              <template v-if="plan.agent_task.test_cases">
                <h4>建议生成的测试用例</h4>
                <ul class="test-cases"><li v-for="test in plan.agent_task.test_cases" :key="test.name"><b>{{ test.name }}</b><span>{{ test.then }}</span></li></ul>
              </template>
            </section>
          </div>

          <section class="advice">
            <h3>{{ plan.agent_task.title }}</h3>
            <div class="advice-columns">
              <div><h4>为什么</h4><ol><li v-for="item in plan.agent_task.reason" :key="item">{{ item }}</li></ol></div>
              <div><h4>具体怎么做</h4><ol><li v-for="item in plan.agent_task.instructions" :key="item">{{ item }}</li></ol></div>
              <div><h4>验收标准</h4><ol><li v-for="item in acceptance(plan)" :key="item">{{ item }}</li></ol></div>
            </div>
          </section>
        </div>
      </article>
    </div>

    <div v-else-if="loaded && !loading && !error" class="empty-state">当前时间窗口没有找到可分析的热点函数。</div>
  </div>
</template>

<script>
export default {
  props: { repoName: String },
  data() {
    return { repositoryMember: '', repositoryMembers: [], techniques: [], technique: 'extract-method', windowDays: 7, previousWindowDays: 0, limit: 5, minTests: 1, plans: [], expanded: {}, loaded: false, editingTechnique: false, editingExisting: false, savingTechnique: false, techniqueError: '', techniqueForm: { id: '', name: '', objective: '', checksText: '' } }
  },
  computed: {
    selectedTechnique() { return this.techniques.find(item => item.id === this.technique) },
    previousWindowOptions() { return [7, 14, 28, 56, 84].filter(days => days < this.windowDays) },
    refactorableCount() { return this.plans.filter(plan => plan.test_gate.refactoring_allowed).length },
    protectedCount() { return this.plans.filter(plan => plan.test_gate.status === 'sufficient').length },
    testFirstCount() { return this.plans.filter(plan => plan.test_gate.status !== 'sufficient').length },
  },
  watch: {
    windowDays() { if (this.previousWindowDays >= this.windowDays) this.previousWindowDays = 0 },
  },
  async created() {
    await this.loadTechniques()
    await this.loadPlans()
  },
  methods: {
    async loadTechniques() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/refactor-techniques', { repo: this.repoName || '', member: this.repositoryMember })
        this.repositoryMember = data.repository_member || this.repositoryMember
        this.repositoryMembers = data.repository_members || []
        this.techniques = data.techniques || []
        if (!this.techniques.some(item => item.id === this.technique)) this.technique = this.techniques[0]?.id || ''
      })
    },
    async onMemberChange() { this.cancelEdit(); await this.loadTechniques(); await this.loadPlans() },
    beginCreate() {
      this.editingTechnique = true; this.editingExisting = false; this.techniqueError = ''
      this.techniqueForm = { id: '', name: '', objective: '', checksText: '' }
    },
    beginEdit(item) {
      this.editingTechnique = true; this.editingExisting = true; this.techniqueError = ''
      this.techniqueForm = { id: item.id, name: item.name, objective: item.objective, checksText: item.checks.join('\n') }
    },
    cancelEdit() { this.editingTechnique = false; this.techniqueError = '' },
    async saveTechnique() {
      const checks = this.techniqueForm.checksText.split(/\n|,/).map(item => item.trim()).filter(Boolean)
      if (!checks.length) { this.techniqueError = '请至少填写一个检查项'; return }
      const body = { id: this.techniqueForm.id, name: this.techniqueForm.name, objective: this.techniqueForm.objective, checks }
      this.savingTechnique = true; this.techniqueError = ''
      try {
        const path = this.editingExisting ? `/api/refactor-techniques/${encodeURIComponent(body.id)}` : '/api/refactor-techniques'
        const saved = await this.$api.request(path, { query: { repo: this.repoName || '', member: this.repositoryMember }, method: this.editingExisting ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        await this.loadTechniques()
        this.technique = saved.id
        this.editingTechnique = false
        await this.loadPlans()
      } catch (error) { this.techniqueError = error.message || String(error) }
      finally { this.savingTechnique = false }
    },
    async loadPlans() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/refactor-plans', {
          repo: this.repoName || '', member: this.repositoryMember, technique: this.technique, window_days: this.windowDays,
          previous_window_days: this.previousWindowDays, limit: this.limit, min_tests: this.minTests,
        })
        this.plans = data.plans || []
        this.expanded = this.plans.length ? { 0: true } : {}
        this.loaded = true
      })
    },
    toggle(index) { this.expanded[index] = !this.expanded[index] },
    location(plan) { return `${plan.hotspot.file_path}:${plan.hotspot.start_line}-${plan.hotspot.end_line}` },
    gateLabel(status) { return ({ sufficient: '测试防护充分', partial: '测试覆盖不完整', missing: '缺少关联测试', unreliable: '测试不可靠' })[status] || status },
    acceptance(plan) { return plan.agent_task.acceptance || plan.agent_task.validation?.required || [] },
  },
}
</script>

<style scoped>
.page-header { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; margin-bottom:16px; }
.page-header h1 { font-size:24px; margin-bottom:5px; }.page-header p { color:#777; font-size:13px; }
button, select, input, textarea { font:inherit; }.header-actions,.form-actions { display:flex;gap:8px }.primary,.secondary { border:0; border-radius:6px; padding:10px 15px; cursor:pointer }.primary { background:#e94560; color:#fff; }.secondary { background:#ececf2;color:#333 }.primary:disabled { opacity:.55; }
.technique-form { margin-bottom:14px;padding:16px;background:#fff;border:1px solid #eadde0;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.06) }.form-heading { display:flex;justify-content:space-between;margin-bottom:13px }.form-heading h2 { font-size:17px }.form-heading p { color:#888;font-size:11px;margin-top:3px }.link { border:0;background:none;color:#999;cursor:pointer }.form-grid { display:grid;grid-template-columns:1fr 1fr;gap:11px }.form-grid label { font-size:11px;color:#666 }.form-grid .wide { grid-column:1/-1 }.form-grid input,.form-grid textarea { display:block;width:100%;margin-top:4px;padding:8px;border:1px solid #ddd;border-radius:5px;resize:vertical }.form-grid input:disabled { background:#f3f3f5;color:#888 }.form-actions { justify-content:flex-end;margin-top:12px }.form-error { margin-top:9px;padding:8px;background:#f8d7da;color:#721c24;border-radius:5px;font-size:11px }
.filters { display:grid; grid-template-columns:repeat(2,2fr) repeat(4, 1fr); gap:10px; padding:14px; background:#fff; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
.filters label { color:#777; font-size:11px; }.filters select,.filters input { display:block; width:100%; margin-top:5px; padding:8px; border:1px solid #ddd; border-radius:5px; background:#fff; }
.technique-summary { display:flex; justify-content:space-between; gap:15px; margin:12px 0; padding:10px 14px; background:#fff8e1; border:1px solid #ffe6a3; border-radius:7px; font-size:12px; }.technique-summary b,.technique-summary span { margin-right:8px; }.technique-tools { display:flex;align-items:center;gap:8px }.chips span { display:inline-block; padding:3px 7px; background:#fff; border-radius:10px; color:#795c12; }.edit-button { border:0;background:transparent;color:#c82d48;cursor:pointer;font-size:11px }
.summary-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:14px 0; }.summary-grid div { background:#fff; padding:14px; border-radius:7px; text-align:center; }.summary-grid strong,.summary-grid span { display:block; }.summary-grid strong { color:#e94560; font-size:24px; }.summary-grid span { color:#888; font-size:11px; }
.plan-list { display:grid; gap:12px; }.plan-card { background:#fff; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.08); overflow:hidden; }.plan-heading { width:100%; display:flex; align-items:center; gap:11px; padding:14px; border:0; background:#fff; text-align:left; cursor:pointer; }.sequence { width:27px; height:27px; border-radius:50%; display:grid; place-items:center; background:#1a1a2e; color:#fff; font-size:11px; }.heading-main { flex:1; min-width:0; }.heading-main b,.heading-main small { display:block; }.heading-main b { overflow:hidden; text-overflow:ellipsis; }.heading-main small { color:#888; margin-top:4px; }.badge,.risk { border-radius:11px; padding:4px 8px; font-size:10px; }.ready { background:#d4edda; color:#155724; }.blocked { background:#fff3cd; color:#856404; }.risk.low { background:#e8f5e9;color:#247237 }.risk.medium { background:#fff3cd;color:#856404 }.risk.high { background:#f8d7da;color:#721c24 }.toggle { color:#e94560;font-size:11px; }
.plan-body { border-top:1px solid #eee; padding:16px; }.evidence-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:14px; }.evidence-grid div { padding:10px; border:1px solid #eee; border-radius:6px; }.evidence-grid span,.evidence-grid strong { display:block; }.evidence-grid span { color:#999;font-size:10px }.evidence-grid strong { margin-top:2px;font-size:18px }.detail-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }.detail-grid>section,.advice { border:1px solid #eee; border-radius:7px; padding:14px; }.plan-body h3 { font-size:14px;margin-bottom:10px }.plan-body h4 { font-size:11px;color:#777;margin:13px 0 6px }.location { display:block;background:#171725;color:#ddd;padding:8px;border-radius:5px;word-break:break-all }.scope-note,.empty { color:#999;font-size:11px;margin-top:7px }.plan-body ul,.plan-body ol { padding-left:18px;margin:0 }.plan-body li { margin:5px 0;font-size:11px }.plan-body li code,.plan-body li span { display:block;color:#888;margin-top:2px;word-break:break-all }.gate { padding:9px;border-radius:6px;margin-bottom:9px }.gate b,.gate span { display:block }.gate span { font-size:10px;margin-top:3px }.gate.sufficient { background:#e8f5e9;color:#247237 }.gate.partial,.gate.missing { background:#fff3cd;color:#856404 }.advice { margin-top:14px;background:#fafafd }.advice-columns { display:grid;grid-template-columns:repeat(3,1fr);gap:14px }.empty-state { padding:60px;text-align:center;color:#999 }
@media(max-width:900px){.filters,.form-grid{grid-template-columns:1fr 1fr}.summary-grid,.evidence-grid{grid-template-columns:1fr 1fr}.detail-grid,.advice-columns{grid-template-columns:1fr}.badge,.risk{display:none}}
</style>
