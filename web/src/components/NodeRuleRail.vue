<template>
  <div class="nrr">
    <template v-if="!target">
      <p class="nrr-hint">{{ t('点击左侧的') }}<em>{{ t('端点根 / 函数 / 跨服务') }}</em>{{ t('节点，在右侧查看 API 节点自身翻译和聚合解释。external 外部调用无可解析源码。') }}</p>
    </template>

    <template v-else-if="target.kind === 'external' || target.kind === 'note'">
      <div class="nrr-head"><b class="nrr-title">{{ target.title }}</b></div>
      <p class="nrr-note">{{ t('外部 / 未匹配调用，无可解析源码，不支持生成业务规则。') }}</p>
    </template>

    <template v-else>
      <div class="nrr-head">
        <b class="nrr-title">{{ target.title }}</b>
        <span v-if="target.subtitle" class="nrr-sub">{{ target.subtitle }}</span>
        <span v-if="status" class="br-status" :class="'br-' + status">{{ statusText }}</span>
      </div>

      <div v-if="explanationMode" class="nrr-api-mode">
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
            <p>{{ explanationSummary(explanationNode.local_explanation) || t('暂无节点自身解释') }}</p>
          </section>
          <details class="nrr-api-card" open v-if="explanationNode.aggregate_explanation">
            <summary>{{ t('节点聚合结果') }}</summary>
            <p>{{ explanationSummary(explanationNode.aggregate_explanation) || t('暂无聚合解释') }}</p>
            <ol v-if="explanationSteps(explanationNode.aggregate_explanation).length">
              <li v-for="(step, index) in explanationSteps(explanationNode.aggregate_explanation)" :key="index">{{ stepText(step) }}</li>
            </ol>
          </details>
        </template>
        <p v-else-if="explanationState.loading" class="nrr-loading">{{ t('正在读取 API 解释快照…') }}</p>
        <p v-else-if="explanationSnapshot" class="nrr-muted">{{ t('当前快照未覆盖该节点，或该节点尚未完成翻译。') }}</p>
        <p v-else class="nrr-muted">{{ t('尚未生成端点级 API 解释。选中端点根后，可在此处手动生成。') }}</p>
      </div>

      <div v-else-if="loading" class="nrr-loading">{{ t('正在加载该节点的业务规则…') }}</div>

      <div v-else-if="!explanationMode">
        <!-- 编辑提示词 + 生成 -->
        <div v-if="editing" class="nrr-edit">
          <textarea
            v-model="editPrompt"
            rows="6"
            class="br-textarea"
            :placeholder="t('可编辑提示词；留空则使用服务器默认提示词（分析该节点源码片段）')"
          ></textarea>
          <div class="br-actions">
            <button class="primary sm" :disabled="genLoading" @click="generate">
              {{ genLoading ? t('生成中…') : t('生成') }}
            </button>
            <button class="secondary sm" :disabled="genLoading" @click="cancelEdit">{{ t('取消') }}</button>
          </div>
        </div>

        <!-- 已生成结果 -->
        <div v-else-if="rule && rule.result" class="nrr-result">
          <div v-if="parsed" class="br-parsed">
            <p class="br-purpose">{{ parsed.business_purpose_zh || parsed.business_purpose_en }}</p>
            <details v-if="parsed.business_flow_zh?.length || parsed.business_flow_en?.length" class="br-detail">
              <summary>{{ t('业务步骤') }}</summary>
              <ol><li v-for="(s, si) in (parsed.business_flow_zh || parsed.business_flow_en || [])" :key="si">{{ s }}</li></ol>
            </details>
            <details v-if="parsed.business_rules?.length" class="br-detail">
              <summary>{{ t('业务规则') }} ({{ parsed.business_rules.length }})</summary>
              <ul><li v-for="(r, ri) in parsed.business_rules" :key="ri">{{ r }}</li></ul>
            </details>
            <details v-if="parsed.side_effects?.length" class="br-detail">
              <summary>{{ t('副作用') }}</summary>
              <ul><li v-for="(e, ei) in parsed.side_effects" :key="ei">{{ e }}</li></ul>
            </details>
          </div>
          <pre v-else class="br-raw">{{ rule.result }}</pre>
          <p v-if="rule.error" class="nrr-error">{{ rule.error }}</p>
          <div class="br-actions">
            <button class="secondary sm" :disabled="genLoading" @click="startEdit">{{ t('编辑提示词') }}</button>
            <button class="secondary sm" :disabled="genLoading" @click="retry">
              {{ genLoading ? t('重试中…') : t('重试') }}
            </button>
          </div>
        </div>

        <!-- 尚无结果 -->
        <div v-else class="nrr-empty">
          <p class="nrr-muted">{{ t('尚未生成该节点的业务规则。') }}</p>
          <button class="primary sm" @click="startEdit">{{ t('生成业务规则') }}</button>
        </div>

        <p v-if="genError" class="nrr-error">{{ genError }}</p>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { apiClient } from '../api/apiClient.js'
import { t } from '../i18n.js'

const props = defineProps({
  repo: { type: String, default: '' },
  member: { type: String, default: '' },
  snapshotId: { type: String, default: '' },
  mermaid: { type: String, default: '' },
  target: { type: Object, default: null },
  explanationMode: { type: Boolean, default: false },
  explanationSnapshot: { type: Object, default: null },
  explanationState: { type: Object, default: () => ({}) },
  explanationNode: { type: Object, default: null },
})

const emit = defineEmits(['generate-api', 'manage-api-explanations'])

const loading = ref(false)
const genLoading = ref(false)
const editing = ref(false)
const rule = ref(null)
const defaultPrompt = ref('')
const editPrompt = ref('')
const genError = ref('')

const status = computed(() => {
  if (genLoading.value) return 'loading'
  return rule.value?.status || ''
})
const statusText = computed(() => {
  if (status.value === 'loading') return t('生成中')
  if (status.value === 'completed') return t('已生成')
  if (status.value === 'failed') return t('失败')
  return ''
})

const parsed = computed(() => {
  const raw = rule.value?.result
  if (!raw) return null
  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === 'object' && !obj.error && (obj.business_purpose_en || obj.business_purpose_zh)) {
      return obj
    }
  } catch { /* fall through */ }
  return null
})

// ── load state for the currently selected node ─────────────────────────────

function empty() {
  loading.value = false
  genLoading.value = false
  editing.value = false
  rule.value = null
  defaultPrompt.value = ''
  editPrompt.value = ''
  genError.value = ''
}

function loadNode() {
  empty()
  if (props.explanationMode) return
  const kind = props.target?.kind
  if (kind === 'root') {
    loadRoot()
  } else if (kind === 'func' || kind === 'cross') {
    loadGraphNode()
  }
}

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

function stepText(step) {
  return typeof step === 'string' ? step : (step.detail || step.summary || step.title || JSON.stringify(step))
}

async function loadGraphNode() {
  loading.value = true
  try {
    const d = props.target.descriptor || {}
    const resp = await apiClient.get('/api/call-tree/rule', {
      snapshot_id: props.snapshotId,
      view_id: '',
      node_type: props.target.node_type || 'func',
      node_id: d.node_id,
      handler: d.handler,
    })
    rule.value = resp.rule || null
    defaultPrompt.value = resp.default_prompt || ''
  } catch (err) {
    genError.value = detail(err)
  } finally {
    loading.value = false
  }
}

async function loadRoot() {
  loading.value = true
  try {
    const meta = props.target.rootMeta || {}
    const data = await apiClient.get('/api/business-rules', { repository_snapshot_id: props.snapshotId })
    const found = (data.rules || []).find(
      (r) => r.handler === meta.handler && r.method === meta.method && r.path === meta.path,
    )
    rule.value = found || null
    defaultPrompt.value = ''
  } catch (err) {
    genError.value = detail(err)
  } finally {
    loading.value = false
  }
}

// ── edit / generate ────────────────────────────────────────────────────────

function startEdit() {
  editing.value = true
  editPrompt.value = rule.value?.custom_prompt || defaultPrompt.value || ''
}

function cancelEdit() {
  editing.value = false
  editPrompt.value = ''
}

async function generate() {
  const kind = props.target?.kind
  if (!kind) return
  genLoading.value = true
  genError.value = ''
  try {
    let resp
    if (kind === 'root') {
      const meta = props.target.rootMeta || {}
      resp = await apiClient.request('/api/business-rules/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repository_snapshot_id: props.snapshotId,
          handler: meta.handler || '',
          method: meta.method || '',
          path: meta.path || '',
          call_chain_mermaid: props.mermaid || '',
          custom_prompt: editPrompt.value,
        }),
      })
    } else {
      const d = props.target.descriptor || {}
      const body = {
        repository_snapshot_id: props.snapshotId,
        node_type: props.target.node_type || 'func',
        custom_prompt: editPrompt.value,
      }
      if (props.target.node_type === 'func') body.node_id = d.node_id
      if (props.target.node_type === 'cross') body.handler = d.handler
      resp = await apiClient.request('/api/call-tree/rule/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    }
    rule.value = {
      result: resp.result || '',
      status: resp.status || 'completed',
      custom_prompt: editPrompt.value,
      error: '',
    }
    editing.value = false
  } catch (err) {
    rule.value = { ...rule.value, result: rule.value?.result || '', status: 'failed', error: detail(err) }
    editing.value = false
  } finally {
    genLoading.value = false
  }
}

function retry() {
  startEdit()
  generate()
}

function detail(err) {
  return (err?.body && (err.body.detail || err.body.message)) || err?.message || t('请求失败')
}

watch(
  () => props.target && `${props.target.kind}|${props.target.identity}`,
  () => loadNode(),
  { immediate: true },
)
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
.nrr-empty { display: grid; gap: 8px; }
.nrr-result { font-size: 12px; }
.nrr-api-mode { display: grid; gap: 8px; }
.nrr-api-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.nrr-api-progress { color: #2a6496; background: #e3f0fc; border-radius: 6px; padding: 7px 8px; margin: 0; line-height: 1.5; }
.nrr-api-meta { display: flex; align-items: center; gap: 6px; color: #8a8f9a; font-size: 10px; }
.nrr-api-card { border: 1px solid #ececf2; border-radius: 6px; padding: 8px 9px; background: #fff; line-height: 1.55; }
.nrr-api-card h4 { margin: 0 0 4px; font-size: 11px; color: #555; }
.nrr-api-card p { margin: 0; color: #454852; }
.nrr-api-card ol { margin: 6px 0 0 16px; padding: 0; color: #555; }
.nrr-api-card summary { cursor: pointer; color: #555; font-weight: 600; }
.nrr-error { color: #c0392b; font-size: 11px; margin: 6px 0 0; word-break: break-word; }
.nrr-edit { display: grid; gap: 8px; }

/* visual parity with Knowledge.vue's business-rule panel */
.br-status { font-size: 10px; padding: 2px 6px; border-radius: 8px; margin-left: 6px; font-weight: 400; }
.br-loading { background: #e3f0fc; color: #2a6496; }
.br-completed { background: #eaf8f0; color: #23764a; }
.br-failed { background: #ffeaea; color: #b8324a; }
.br-textarea { width: 100%; border: 1px solid #cfd3da; border-radius: 5px; padding: 8px; font: 11px/1.5 monospace; resize: vertical; }
.br-parsed { display: grid; gap: 6px; }
.br-purpose { color: #333; line-height: 1.5; margin: 0; }
.br-detail { font-size: 11px; }
.br-detail summary { color: #555; cursor: pointer; padding: 3px 0; }
.br-detail ol, .br-detail ul { margin: 4px 0 4px 18px; }
.br-detail li { padding: 2px 0; color: #444; line-height: 1.4; }
.br-raw { max-height: 200px; overflow: auto; font-size: 10px; padding: 8px; background: #f5f5f8; border-radius: 4px; white-space: pre-wrap; }
.br-actions { display: flex; gap: 6px; margin-top: 4px; }
.primary { background: #e94560; color: white; }
.secondary { background: #ececf2; color: #333; }
button { border: 0; border-radius: 6px; padding: 6px 10px; font-size: 11px; cursor: pointer; }
button:disabled { opacity: 0.55; cursor: default; }
.sm { padding: 5px 10px; font-size: 11px; }
</style>
