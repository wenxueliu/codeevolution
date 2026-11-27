<template>
  <div class="rule-map" data-testid="rule-map">
    <UiState v-if="loading" kind="loading" :title="t('正在加载业务规则地图')" />
    <UiState v-else-if="error" kind="error" :title="t('加载规则地图失败')" :message="error" :action-label="t('重试')" @action="load" />
    <UiState v-else-if="map && !hasRules" kind="empty" :title="t('暂无规则')" :message="t('该 Snapshot 还没有可聚合的接口业务规则。请先在「API 契约」中生成端点解释。')" />
    <template v-else-if="map">
      <div class="rule-metrics">
        <div class="metric">{{ summary.interface_count || 0 }} <small>{{ t('个接口') }}</small></div>
        <div class="metric">{{ summary.interfaces_with_rules || 0 }} <small>{{ t('个有规则') }}</small></div>
        <div class="metric">{{ Math.round((summary.coverage || 0) * 100) }}% <small>{{ t('覆盖率') }}</small></div>
        <div class="metric">{{ summary.group_count || 0 }} <small>{{ t('个分组') }}</small></div>
        <div class="metric">{{ summary.rule_count || 0 }} <small>{{ t('条规则') }}</small></div>
        <div class="metric">{{ summary.shared_rule_count || 0 }} <small>{{ t('条共享') }}</small></div>
      </div>

      <div class="rule-toolbar">
        <div class="rule-tabs">
          <button type="button" :class="{ active: mode === 'rules' }" @click="mode = 'rules'">{{ t('服务规则') }}</button>
          <button type="button" :class="{ active: mode === 'integration' }" @click="mode = 'integration'">{{ t('跨服务集成') }} ({{ summary.integration_count || 0 }})</button>
        </div>
        <label class="rule-search">
          <input v-model.trim="search" type="search" :placeholder="t('搜索规则、分组或接口')" />
        </label>
        <label v-if="mode === 'rules'" class="rule-select">
          <select v-model="typeFilter">
            <option value="">{{ t('全部类型') }}</option>
            <option v-for="type in ruleTypes" :key="type" :value="type">{{ t(type) }}</option>
          </select>
        </label>
        <label v-if="mode === 'rules'" class="rule-check">
          <input v-model="sharedOnly" type="checkbox" /> {{ t('仅共享规则') }}
        </label>
        <button type="button" class="secondary sm" :disabled="exporting" @click="exportMap">{{ t('导出规则地图') }}</button>
      </div>

      <div v-if="mode === 'rules'" class="rule-columns">
        <aside class="rule-groups">
          <button type="button" class="rule-group" :class="{ active: !selectedGroup }" @click="selectGroup('')">
            <span>{{ t('全部规则') }}</span><small>{{ summary.rule_count || 0 }}</small>
          </button>
          <button v-for="group in groups" :key="group.group_key" type="button" class="rule-group" :class="{ active: selectedGroup === group.group_key }" @click="selectGroup(group.group_key)">
            <span>{{ group.group_key }}</span><small>{{ group.rule_count }}</small>
          </button>
        </aside>

        <section class="rule-list">
          <div class="rule-list-head">
            <div>
              <h3>{{ selectedGroup || t('服务级规则（去重汇总）') }}</h3>
              <p v-if="selectedGroup && activeGroup">
                {{ t('{count} 个接口', { count: activeGroup.interface_count }) }} · {{ t('覆盖率') }} {{ Math.round((activeGroup.coverage || 0) * 100) }}%
              </p>
            </div>
            <span>{{ visibleRules.length }} {{ t('条') }}</span>
          </div>

          <div v-if="selectedGroup && groupInterfaces.length" class="rule-interfaces">
            <button v-for="item in groupInterfaces" :key="item.interface_key" type="button" class="rule-interface" :class="{ active: selectedInterface === item.interface_key }" @click="selectInterface(item.interface_key)">
              <code>{{ item.method }} {{ item.path }}</code><small>{{ item.rule_count }}</small>
            </button>
            <button v-if="selectedInterface" type="button" class="rule-interface clear" @click="selectInterface('')">{{ t('清除接口筛选') }}</button>
          </div>

          <div v-if="visibleRules.length" class="rule-cards">
            <RuleCard v-for="rule in visibleRules" :key="rule.atom_id" :rule="rule" :show-groups="!selectedGroup" />
          </div>
          <UiState v-else kind="empty" :title="t('暂无规则')" :message="t('该分组暂无规则')" />
        </section>
      </div>

      <template v-else>
        <div class="integration-summary">
          <span class="integration-chip cross">{{ t('跨服务') }} {{ summary.cross_service_integration_count || 0 }}</span>
          <span v-for="kind in integrationKinds" :key="kind.kind" class="integration-chip">{{ kindLabel(kind.kind) }} {{ kind.count }}</span>
          <label class="rule-select">
            <select v-model="kindFilter">
              <option value="">{{ t('全部类型') }}</option>
              <option v-for="kind in integrationKinds" :key="kind.kind" :value="kind.kind">{{ kind.kind }} · {{ kindLabel(kind.kind) }}</option>
            </select>
          </label>
        </div>
        <div v-if="visibleIntegrations.length" class="integration-grid">
          <article v-for="item in visibleIntegrations" :key="item.integration_id" class="integration-card" :class="item.scope">
            <header>
              <span class="integration-kind">{{ item.kind }}</span>
              <span class="integration-scope">{{ item.scope === 'cross_service' ? t('跨服务') : t('资源集成') }}</span>
              <code class="integration-target">{{ item.target }}</code>
            </header>
            <dl>
              <div v-if="item.operation"><dt>{{ t('操作') }}</dt><dd><code>{{ item.operation }}</code></dd></div>
              <div v-if="item.input_or_key"><dt>{{ t('输入/Key') }}</dt><dd>{{ item.input_or_key }}</dd></div>
              <div v-if="item.result_usage"><dt>{{ t('结果用途') }}</dt><dd>{{ item.result_usage }}</dd></div>
            </dl>
            <footer>
              <code>{{ item.method }} {{ item.path }}</code>
              <span>{{ item.handler }}</span>
              <small>{{ item.file }}:{{ item.line }}<template v-if="item.evidence_lines?.length"> · {{ t('源码行') }} {{ item.evidence_lines.join(', ') }}</template></small>
            </footer>
          </article>
        </div>
        <UiState v-else kind="empty" :title="t('暂无规则')" :message="t('没有匹配的集成契约')" />
      </template>
    </template>
  </div>
</template>

<script>
import { t } from '../i18n.js'
import RuleCard from './RuleCard.vue'
import UiState from './UiState.vue'

export default {
  name: 'RuleMap',
  components: { RuleCard, UiState },
  props: {
    snapshotId: { type: String, required: true },
    repoName: { type: String, default: '' },
  },
  data() {
    return {
      map: null,
      loading: false,
      error: '',
      mode: 'rules',
      selectedGroup: '',
      selectedInterface: '',
      search: '',
      typeFilter: '',
      kindFilter: '',
      sharedOnly: false,
      exporting: false,
    }
  },
  computed: {
    summary() { return this.map?.summary || {} },
    groups() { return this.map?.groups || [] },
    serviceRules() { return this.map?.service?.rules || [] },
    activeGroup() { return this.groups.find(group => group.group_key === this.selectedGroup) || null },
    groupInterfaces() { return this.activeGroup?.interfaces || [] },
    ruleTypes() { return [...new Set(this.serviceRules.map(rule => rule.rule_type).filter(Boolean))].sort() },
    baseRules() {
      if (!this.selectedGroup) return this.serviceRules
      return this.serviceRules.filter(rule => (rule.groups || []).includes(this.selectedGroup))
    },
    visibleRules() { return this.baseRules.filter(rule => this.matchesRule(rule)) },
    integrationItems() { return this.map?.integrations?.items || [] },
    integrationKinds() { return this.map?.integrations?.by_kind || [] },
    visibleIntegrations() { return this.integrationItems.filter(item => this.matchesIntegration(item)) },
    hasRules() { return (this.summary.rule_count || 0) > 0 || (this.summary.integration_count || 0) > 0 },
  },
  watch: {
    snapshotId() { this.load() },
  },
  mounted() { this.load() },
  methods: {
    t,
    async load() {
      if (!this.snapshotId) return
      this.loading = true
      this.error = ''
      try {
        this.map = await this.$api.get('/api/rule-map', { snapshot_id: this.snapshotId })
        this.selectedGroup = ''
        this.selectedInterface = ''
      } catch (err) {
        this.error = (err.body && (err.body.detail || err.body.message)) || err.message || this.t('加载规则地图失败')
      } finally {
        this.loading = false
      }
    },
    selectGroup(key) {
      this.selectedGroup = this.selectedGroup === key ? '' : key
      this.selectedInterface = ''
    },
    selectInterface(key) {
      this.selectedInterface = this.selectedInterface === key ? '' : key
    },
    matchesRule(rule) {
      if (this.typeFilter && rule.rule_type !== this.typeFilter) return false
      if (this.sharedOnly && rule.role !== 'shared') return false
      if (this.selectedInterface && !(rule.source_interfaces || []).includes(this.selectedInterface)) return false
      const query = this.search.trim().toLowerCase()
      if (!query) return true
      return [rule.text, rule.rule_type, rule.condition, rule.failure_mode,
        ...(rule.groups || []), ...(rule.source_interfaces || [])]
        .some(value => String(value || '').toLowerCase().includes(query))
    },
    matchesIntegration(item) {
      if (this.kindFilter && item.kind !== this.kindFilter) return false
      const query = this.search.trim().toLowerCase()
      if (!query) return true
      return [item.target, item.operation, item.input_or_key, item.result_usage,
        item.method, item.path, item.handler]
        .some(value => String(value || '').toLowerCase().includes(query))
    },
    kindLabel(kind) {
      if (['http', 'rpc', 'message'].includes(kind)) return this.t('HTTP / RPC / 消息')
      if (['database', 'cache', 'file'].includes(kind)) return this.t('数据库 / 缓存 / 文件')
      return this.t('外部 / 其他')
    },
    exportMap() {
      if (!this.map) return
      this.exporting = true
      try {
        const blob = new Blob([JSON.stringify(this.map, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = `rule-map-${this.snapshotId}.json`
        link.click()
        URL.revokeObjectURL(url)
      } finally {
        this.exporting = false
      }
    },
  },
}
</script>

<style scoped>
.rule-map { display: grid; gap: 14px; }
.rule-metrics { display: flex; flex-wrap: wrap; gap: 18px; }
.rule-metrics .metric { margin-bottom: 0; }
.rule-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 0; border-top: 1px solid #eef0f4; border-bottom: 1px solid #eef0f4; }
.rule-tabs { display: flex; gap: 4px; }
.rule-tabs button { border: 1px solid #dfe3ea; background: #fff; color: #4d5665; border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
.rule-tabs button.active { border-color: #e94560; color: #e94560; background: #fff0f2; }
.rule-search { flex: 1 1 180px; min-width: 160px; }
.rule-search input { width: 100%; border: 1px solid #dfe3ea; border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.rule-select select { border: 1px solid #dfe3ea; border-radius: 6px; padding: 6px 8px; font-size: 12px; background: #fff; }
.rule-check { display: flex; align-items: center; gap: 5px; color: #4d5665; font-size: 12px; }
.rule-columns { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 16px; align-items: start; }
.rule-groups { display: grid; gap: 4px; border: 1px solid #eef0f4; border-radius: 8px; padding: 8px; background: #fafbfd; }
.rule-group { display: flex; align-items: center; justify-content: space-between; gap: 8px; border: 1px solid transparent; background: transparent; border-radius: 6px; padding: 7px 9px; cursor: pointer; color: #4d5665; font-size: 12px; text-align: left; }
.rule-group:hover { background: #f1f4f9; }
.rule-group.active { border-color: #e94560; background: #fff0f2; color: #c82d48; }
.rule-group small { color: #8a94a6; }
.rule-list { min-width: 0; display: grid; gap: 10px; }
.rule-list-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.rule-list-head h3 { margin: 0; font-size: 14px; color: #252b36; }
.rule-list-head p { margin: 3px 0 0; color: #8a94a6; font-size: 12px; }
.rule-list-head > span { color: #8a94a6; font-size: 12px; }
.rule-interfaces { display: flex; flex-wrap: wrap; gap: 6px; }
.rule-interface { display: flex; align-items: center; gap: 6px; border: 1px solid #dfe3ea; background: #fff; border-radius: 14px; padding: 3px 9px; cursor: pointer; font-size: 11px; color: #4d5665; }
.rule-interface.active { border-color: #e94560; color: #c82d48; background: #fff0f2; }
.rule-interface.clear { border-style: dashed; }
.rule-interface small { color: #8a94a6; }
.rule-cards { display: grid; gap: 8px; }
.integration-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.integration-chip { border-radius: 12px; background: #eef1f6; color: #55637a; padding: 3px 9px; font-size: 11px; }
.integration-chip.cross { background: #fff0f2; color: #c82d48; }
.integration-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }
.integration-card { border: 1px solid #e2e5eb; border-radius: 8px; padding: 12px; background: #fff; display: grid; gap: 8px; }
.integration-card.cross_service { border-left: 3px solid #e94560; }
.integration-card header { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.integration-kind { font-size: 10px; text-transform: uppercase; background: #eef1f6; color: #55637a; border-radius: 3px; padding: 1px 5px; }
.integration-scope { font-size: 11px; color: #c82d48; }
.integration-target { margin-left: auto; font-size: 12px; color: #252b36; word-break: break-all; }
.integration-card dl { margin: 0; display: grid; gap: 3px; font-size: 12px; }
.integration-card dl div { display: flex; gap: 8px; }
.integration-card dt { color: #8a94a6; min-width: 62px; }
.integration-card dd { margin: 0; color: #4d5665; }
.integration-card footer { border-top: 1px dashed #e6e9ef; padding-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; font-size: 11px; color: #8a94a6; }
.integration-card footer code { color: #4d5665; }
@media (max-width: 900px) {
  .rule-columns { grid-template-columns: minmax(0, 1fr); }
  .rule-groups { grid-auto-flow: column; grid-auto-columns: max-content; overflow-x: auto; }
}
</style>
