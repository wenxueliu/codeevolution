<template>
  <section class="node-source-panel">
    <div class="nsp-heading">
      <h3>{{ t('节点源码') }}</h3>
      <span v-if="source?.file" class="nsp-location">
        {{ source.file }}:{{ source.start_line }}<span v-if="source.end_line && source.end_line !== source.start_line">-{{ source.end_line }}</span>
      </span>
    </div>

    <p v-if="!target" class="nsp-muted">{{ t('点击调用链节点查看源码。') }}</p>
    <p v-else-if="target.kind === 'external' || target.kind === 'note'" class="nsp-note">
      {{ t('外部 / 未匹配调用，无可解析源码。') }}
    </p>
    <p v-else-if="loading" class="nsp-muted">{{ t('正在读取节点源码…') }}</p>
    <p v-else-if="error" class="nsp-error">{{ error }}</p>
    <p v-else-if="!source?.content" class="nsp-muted">{{ t('该节点源码不可用，可能未纳入当前 Snapshot。') }}</p>
    <div v-else class="nsp-code" role="region" :aria-label="t('节点源码')">
      <div v-for="(line, index) in lines" :key="index" class="nsp-code-line">
        <span class="nsp-line-number">{{ source.start_line + index }}</span>
        <code>{{ line || ' ' }}</code>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { apiClient } from '../api/apiClient.js'
import { t } from '../i18n.js'

const props = defineProps({
  snapshotId: { type: String, default: '' },
  target: { type: Object, default: null },
})

const source = ref(null)
const loading = ref(false)
const error = ref('')
let requestId = 0

const lines = computed(() => (source.value?.content || '').split('\n'))

function reset() {
  requestId += 1
  source.value = null
  loading.value = false
  error.value = ''
}

function nodeIdFor(target) {
  if (target?.kind === 'root') return target.rootMeta?.node_id || ''
  if (target?.kind === 'func') return target.descriptor?.node_id || ''
  return ''
}

async function load() {
  reset()
  const nodeId = nodeIdFor(props.target)
  if (!props.snapshotId || !nodeId) return

  const currentRequest = ++requestId
  loading.value = true
  try {
    const response = await apiClient.get('/api/call-tree/rule', {
      snapshot_id: props.snapshotId,
      node_id: nodeId,
      view_id: '',
    })
    if (currentRequest === requestId) source.value = response.source || null
  } catch (err) {
    if (currentRequest === requestId) error.value = detail(err)
  } finally {
    if (currentRequest === requestId) loading.value = false
  }
}

function detail(err) {
  return (err?.body && (err.body.detail || err.body.message)) || err?.message || t('请求失败')
}

watch(() => props.target && `${props.target.kind}|${props.target.identity}`, load, { immediate: true })
</script>

<style scoped>
.node-source-panel { display: grid; gap: 7px; font-size: 12px; }
.nsp-heading { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; }
.nsp-heading h3 { margin: 0; color: #555; font-size: 12px; }
.nsp-location { color: #8a8f9a; font: 10px/1.4 monospace; word-break: break-all; }
.nsp-muted, .nsp-note, .nsp-error { margin: 0; font-size: 11px; line-height: 1.5; }
.nsp-muted { color: #8a8f9a; }
.nsp-note { color: #b98a00; background: #fff8e1; border-radius: 6px; padding: 8px 10px; }
.nsp-error { color: #c0392b; word-break: break-word; }
.nsp-code { max-height: 410px; overflow: auto; padding: 7px 0; background: #f5f5f8; border: 1px solid #ececf2; border-radius: 5px; font: 10px/1.5 monospace; tab-size: 2; }
.nsp-code-line { display: grid; grid-template-columns: 42px minmax(0, 1fr); padding-right: 8px; white-space: pre; }
.nsp-line-number { color: #a0a6b1; text-align: right; padding-right: 9px; user-select: none; }
.nsp-code-line code { color: #343740; overflow-wrap: normal; font: inherit; }
</style>
