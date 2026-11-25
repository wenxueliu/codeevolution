<template>
  <div class="nrr">
    <template v-if="!target">
      <p class="nrr-hint">{{ t('点击左侧的') }}<em>{{ t('端点根 / 函数 / 跨服务') }}</em>{{ t('节点，在右侧查看 API 节点自身翻译和聚合解释。external 外部调用无可解析源码。') }}</p>
    </template>

    <template v-else-if="target.kind === 'external' || target.kind === 'note'">
      <div class="nrr-head"><b class="nrr-title">{{ target.title }}</b></div>
      <p class="nrr-note">{{ t('外部 / 未匹配调用，无可解析源码，无法生成节点解释。') }}</p>
    </template>

    <template v-else>
      <div class="nrr-head">
        <b class="nrr-title">{{ target.title }}</b>
        <span v-if="target.subtitle" class="nrr-sub">{{ target.subtitle }}</span>
      </div>

      <div class="nrr-api-mode">
        <div v-if="target.kind === 'root'" class="nrr-api-actions">
          <button class="primary sm" :disabled="explanationState.running" @click="emit('generate-api')">
            {{ explanationState.running ? t('生成中…') : (explanationSnapshot ? t('手动刷新 API 解释') : t('生成 API 功能解释')) }}
          </button>
          <button class="secondary sm" @click="emit('manage-api-explanations')">{{ explanationState.showSnapshots ? t('收起快照') : t('管理快照') }}</button>
        </div>
        <p v-if="explanationState.running" class="nrr-api-progress">{{ t('正在按调用链从叶子节点翻译并向入口聚合，当前快照仍可查看。') }}</p>
        <template v-if="explanationNode">
          <div class="nrr-api-meta">
            <span class="br-status" :class="'br-' + (explanationNode.status || 'pending')">{{ apiStatusText(explanationNode.status) }}</span>
            <span v-if="explanationSnapshot">{{ t('快照') }} {{ explanationSnapshot.id }}</span>
          </div>
          <section class="nrr-api-card">
            <h4>{{ t('节点自身翻译') }}</h4>
            <p>{{ explanationSummary(explanationNode.local_explanation) || nodeExplanationFallback(explanationNode, 'local') }}</p>
          </section>
          <details class="nrr-api-card" open v-if="explanationNode.aggregate_explanation">
            <summary>{{ t('节点聚合结果') }}</summary>
            <p>{{ explanationSummary(explanationNode.aggregate_explanation) || nodeExplanationFallback(explanationNode, 'aggregate') }}</p>
            <ol v-if="explanationSteps(explanationNode.aggregate_explanation).length">
              <li v-for="(step, index) in explanationSteps(explanationNode.aggregate_explanation)" :key="index">{{ stepText(step) }}</li>
            </ol>
          </details>
        </template>
        <p v-else-if="explanationState.loading" class="nrr-loading">{{ t('正在读取 API 解释快照…') }}</p>
        <p v-else-if="explanationSnapshot" class="nrr-muted">{{ t('当前快照未覆盖该节点，或该节点尚未完成翻译。') }}</p>
        <p v-else class="nrr-muted">{{ t('尚未生成端点级 API 解释。选中端点根后，可在此处手动生成。') }}</p>
      </div>
    </template>
  </div>
</template>

<script setup>
import { t } from '../i18n.js'

const props = defineProps({
  target: { type: Object, default: null },
  explanationSnapshot: { type: Object, default: null },
  explanationState: { type: Object, default: () => ({}) },
  explanationNode: { type: Object, default: null },
})

const emit = defineEmits(['generate-api', 'manage-api-explanations'])

function apiStatusText(value) {
  return t(({ pending: '等待中', running: '生成中', completed: '已完成', partial: '部分完成', failed: '失败' })[value] || value || '未生成')
}

function explanationObject(value) {
  if (!value) return null
  if (typeof value === 'object') return value
  try { return JSON.parse(value) } catch { return { summary: value } }
}

function explanationSummary(value) {
  const parsed = explanationObject(value) || {}
  return parsed.summary || parsed.business_purpose_zh || parsed.business_purpose_en || parsed.purpose || ''
}

function explanationSteps(value) {
  const parsed = explanationObject(value) || {}
  return parsed.main_flow || parsed.business_flow || parsed.business_flow_zh || parsed.steps || []
}

function nodeExplanationFallback(node, kind) {
  const issues = props.explanationSnapshot?.coverage?.issues || []
  const issue = issues.find((item) => item.node_key === node.node_key)
  if (issue?.code === 'source_incomplete') return t('该节点源码未完整纳入快照，无法生成完整解释')
  if (issue?.code === 'local_explanation_missing') return t('该节点没有可用源码片段，模型无法生成解释')
  if (issue?.code === 'aggregate_explanation_missing' || kind === 'aggregate') return t('模型未返回该节点的聚合解释')
  return t('模型未返回可显示的节点解释')
}

function stepText(step) {
  return typeof step === 'string'
    ? step
    : (step.detail || step.action || step.flow || step.rule || step.description || step.summary || step.title || step.text || JSON.stringify(step))
}
</script>

<style scoped>
.nrr { font-size: 12px; display: grid; gap: 8px; }
.nrr-hint { color: #8a8f9a; font-size: 11px; line-height: 1.6; margin: 0; }
.nrr-hint em { color: #5c5f68; font-style: normal; font-weight: 600; }
.nrr-note { color: #b98a00; font-size: 11px; background: #fff8e1; border-radius: 6px; padding: 8px 10px; margin: 0; }
.nrr-head { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.nrr-title { color: #333; font-size: 12px; }
.nrr-sub { color: #a0a6b1; font-size: 10px; word-break: break-all; }
.nrr-loading, .nrr-muted { color: #8a8f9a; font-size: 11px; margin: 0; }
.nrr-api-mode { display: grid; gap: 8px; }
.nrr-api-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.nrr-api-progress { color: #2a6496; background: #e3f0fc; border-radius: 6px; padding: 7px 8px; margin: 0; line-height: 1.5; }
.nrr-api-meta { display: flex; align-items: center; gap: 6px; color: #8a8f9a; font-size: 10px; }
.nrr-api-card { border: 1px solid #ececf2; border-radius: 6px; padding: 8px 9px; background: #fff; line-height: 1.55; }
.nrr-api-card h4 { margin: 0 0 4px; font-size: 11px; color: #555; }
.nrr-api-card p { margin: 0; color: #454852; }
.nrr-api-card ol { margin: 6px 0 0 16px; padding: 0; color: #555; }
.nrr-api-card summary { cursor: pointer; color: #555; font-weight: 600; }

/* visual parity with Knowledge.vue's business-rule panel */
.br-status { font-size: 10px; padding: 2px 6px; border-radius: 8px; margin-left: 6px; font-weight: 400; }
.br-loading { background: #e3f0fc; color: #2a6496; }
.br-completed { background: #eaf8f0; color: #23764a; }
.br-failed { background: #ffeaea; color: #b8324a; }
.primary { background: #e94560; color: white; }
.secondary { background: #ececf2; color: #333; }
button { border: 0; border-radius: 6px; padding: 6px 10px; font-size: 11px; cursor: pointer; }
button:disabled { opacity: 0.55; cursor: default; }
.sm { padding: 5px 10px; font-size: 11px; }
@media (max-width: 720px) {
  .nrr-api-actions button { min-height: 36px; }
  .nrr-api-card { padding: 9px 10px; }
  .nrr-title, .nrr-sub { overflow-wrap: anywhere; }
}
</style>
