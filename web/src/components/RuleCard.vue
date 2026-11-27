<template>
  <article class="rule-card" :class="[`rule-type-${typeClass}`, { shared: rule.role === 'shared' }]">
    <header class="rule-card-head">
      <span class="rule-type-badge">{{ t(rule.rule_type || 'business') }}</span>
      <span v-if="rule.role === 'shared'" class="rule-role shared">{{ t('共享') }}</span>
      <span v-else class="rule-role">{{ t('本地') }}</span>
      <span v-if="showGroups && rule.groups?.length" class="rule-groups">{{ rule.groups.join(' · ') }}</span>
    </header>
    <p class="rule-text">{{ rule.text }}</p>
    <dl v-if="rule.condition || rule.failure_mode" class="rule-facts">
      <div v-if="rule.condition"><dt>{{ t('条件') }}</dt><dd><code>{{ rule.condition }}</code></dd></div>
      <div v-if="rule.failure_mode"><dt>{{ t('失败模式') }}</dt><dd>{{ rule.failure_mode }}</dd></div>
    </dl>
    <details v-if="rule.evidence?.length" class="rule-evidence">
      <summary>{{ t('证据与来源（{count}）', { count: rule.evidence.length }) }}</summary>
      <div v-for="(item, index) in rule.evidence" :key="index" class="rule-evidence-row">
        <div>
          <code>{{ item.method }} {{ item.path }}</code>
          <span class="rule-handler">{{ item.handler }}</span>
        </div>
        <small>
          {{ item.file }}:{{ item.line }}
          <template v-if="item.lines?.length"> · {{ t('源码行') }} {{ item.lines.join(', ') }}</template>
        </small>
      </div>
    </details>
  </article>
</template>

<script>
import { t } from '../i18n.js'

export default {
  name: 'RuleCard',
  props: {
    rule: { type: Object, required: true },
    showGroups: { type: Boolean, default: false },
  },
  methods: { t },
  computed: {
    typeClass() {
      const value = String(this.rule.rule_type || 'business').toLowerCase()
      return ['validation', 'authorization', 'workflow', 'calculation', 'transformation',
        'consistency', 'contract'].includes(value) ? value : 'business'
    },
  },
}
</script>

<style scoped>
.rule-card { border: 1px solid #e2e5eb; border-left: 3px solid #c9ced8; border-radius: 8px; padding: 12px 14px; background: #fff; }
.rule-card.shared { border-left-color: #2f7de1; background: #fbfdff; }
.rule-type-validation { border-left-color: #2f7de1; }
.rule-type-authorization { border-left-color: #d1435b; }
.rule-type-workflow { border-left-color: #7a5cd6; }
.rule-type-calculation { border-left-color: #2e9b6b; }
.rule-type-transformation { border-left-color: #b0791f; }
.rule-type-consistency { border-left-color: #d1782f; }
.rule-type-contract { border-left-color: #1b8a9a; }
.rule-card-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.rule-type-badge { padding: 1px 7px; border-radius: 10px; background: #eef1f6; color: #55637a; font-size: 11px; }
.rule-role { font-size: 11px; color: #8a94a6; }
.rule-role.shared { color: #2f7de1; font-weight: 600; }
.rule-groups { margin-left: auto; color: #8a94a6; font-size: 11px; }
.rule-text { margin: 0 0 8px; color: #252b36; font-size: 13px; line-height: 1.6; }
.rule-facts { margin: 0 0 8px; display: grid; gap: 4px; font-size: 12px; }
.rule-facts div { display: flex; gap: 8px; }
.rule-facts dt { color: #8a94a6; min-width: 56px; }
.rule-facts dd { margin: 0; color: #4d5665; }
.rule-facts code { background: #f4f6fa; padding: 1px 4px; border-radius: 3px; }
.rule-evidence { border-top: 1px dashed #e6e9ef; padding-top: 8px; }
.rule-evidence summary { cursor: pointer; color: #2f7de1; font-size: 12px; }
.rule-evidence-row { display: flex; flex-direction: column; gap: 2px; padding: 6px 0 0 10px; }
.rule-evidence-row code { font-size: 12px; color: #252b36; }
.rule-handler { margin-left: 8px; color: #8a94a6; font-size: 11px; }
.rule-evidence-row small { color: #8a94a6; font-size: 11px; }
</style>
