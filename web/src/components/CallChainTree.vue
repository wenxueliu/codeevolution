<template>
  <div ref="splitRef" class="call-chain-tree">
    <div v-if="rootError" class="ct-error">{{ rootError }}</div>

    <div v-else class="ct-layout">
      <!-- 上方：端点根 + 调用链树 -->
      <section class="ct-column ct-call-chain">
        <div class="ct-column-title">{{ t('后端调用链') }}</div>
        <div
          class="ct-root"
          :class="{ 'ct-root-selected': selected?.kind === 'root' }"
          role="button"
          tabindex="0"
          :title="t('选中端点根，在右侧查看/生成其整体业务规则')"
          @click="selectRoot"
          @keydown.enter="selectRoot"
        >
          <span class="ct-badge ct-badge-method">{{ rootLabel.method }}</span>
          <code class="ct-path">{{ rootLabel.path }}</code>
          <span class="ct-dim" v-if="rootLabel.handler"> → {{ rootLabel.handler }}</span>
        </div>

        <el-tree
          lazy
          node-key="__key"
          :data="[]"
          :props="treeProps"
          :load="loadNode"
          :expand-on-click-node="true"
          highlight-current
          :empty-text="emptyText"
          class="ct-tree"
          @node-click="onNodeClick"
        >
          <template #default="{ data }">
            <span class="ct-row" :class="{ 'ct-row-clickable': rowClickable(data.type) }">
              <template v-if="data.type === 'func'">
                <span class="ct-fn-dot"></span>
                <code class="ct-name" :title="data.qualified_name || data.name">{{ data.name }}</code>
                <span v-if="data.cycle" class="ct-cycle">↺ {{ t('回环') }}</span>
                <span class="ct-dim" v-if="data.file">{{ data.file }}:{{ data.line }}</span>
              </template>

              <template v-else-if="data.type === 'cross'">
                <span class="ct-badge">{{ data.http_method || 'HTTP' }}</span>
                <code class="ct-path">{{ data.path }}</code>
                <span class="ct-arrow">→</span>
                <b class="ct-svc">{{ data.target_service }}</b>
                <span class="ct-dim">::{{ data.target_function || data.url_pattern }}</span>
                <span v-if="data.cycle" class="ct-cycle">↺ {{ t('回环') }}</span>
              </template>

              <template v-else-if="data.type === 'external'">
                <span class="ct-badge ct-badge-ext">{{ data.http_method || 'HTTP' }}</span>
                <code class="ct-name">{{ data.name }}</code>
                <span class="ct-dim" v-if="data.url">{{ data.url }}</span>
                <span class="ct-note">{{ data.note || t('外部 / 未匹配') }}</span>
              </template>

              <span v-else-if="data.type === 'note'" class="ct-note">{{ data.name }}</span>
            </span>
          </template>
        </el-tree>
      </section>

      <div class="ct-row-divider" aria-hidden="true"></div>

      <!-- 下方：节点解释与节点源码并列 -->
      <div class="ct-details" :style="detailsStyle">
        <aside class="ct-column ct-rail">
          <div class="ct-column-title">{{ t('节点解释与聚合') }}</div>
          <NodeRuleRail
            :repo="repo"
            :member="member"
            :snapshot-id="snapshotId"
            :mermaid="mermaid"
            :target="selected"
            :explanation-mode="explanationMode"
            :explanation-snapshot="explanationSnapshot"
            :explanation-state="explanationState"
            :explanation-node="selectedExplanation"
            @generate-api="emit('generate-api')"
            @manage-api-explanations="emit('manage-api-explanations')"
          />
        </aside>

        <div
          class="ct-divider"
          role="separator"
          tabindex="0"
          data-testid="call-chain-divider-0"
          :aria-label="dividerLabel(0)"
          :aria-valuenow="columnWidths[0]"
          aria-valuemin="20"
          aria-valuemax="80"
          @pointerdown="startResize(0, $event)"
          @keydown.left.prevent="resizeDivider(0, -2)"
          @keydown.right.prevent="resizeDivider(0, 2)"
          @dblclick="resetWidths"
        ></div>

        <aside class="ct-column ct-source-rail">
          <NodeSourcePanel :snapshot-id="snapshotId" :target="selected" />
        </aside>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { apiClient } from '../api/apiClient.js'
import NodeRuleRail from './NodeRuleRail.vue'
import NodeSourcePanel from './NodeSourcePanel.vue'
import { t } from '../i18n.js'

const props = defineProps({
  repo: { type: String, default: '' },
  member: { type: String, default: '' },
  snapshotId: { type: String, default: '' },
  file: { type: String, default: '' },
  line: { type: [String, Number], default: null },
  label: { type: Object, default: () => ({}) },
  mermaid: { type: String, default: '' },
  explanationMode: { type: Boolean, default: false },
  explanationSnapshot: { type: Object, default: null },
  explanationState: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['generate-api', 'manage-api-explanations'])

const treeProps = { label: 'name', isLeaf: 'leaf' }

let seq = 0
const rootLoaded = ref(false)
const rootEmpty = ref(false)
const rootError = ref('')

const selected = ref(null) // { kind, identity, title, subtitle, node_type, descriptor, rootMeta }
const splitRef = ref(null)
const columnWidths = ref([55, 45])
let resizeState = null

const detailsStyle = computed(() => ({
  gridTemplateColumns: `minmax(0, ${columnWidths.value[0]}fr) 10px minmax(0, ${columnWidths.value[1]}fr)`,
}))

const rootLabel = computed(() => ({
  method: props.label.method || 'HTTP',
  path: props.label.path || '',
  handler: props.label.handler || '',
}))

const emptyText = computed(() => {
  if (!rootLoaded.value) return t('正在加载调用链…')
  return rootEmpty.value ? t('该处理函数未识别到直接子调用。') : ''
})

const selectedExplanation = computed(() => {
  const nodeId = selected.value?.kind === 'root'
    ? props.label.node_id
    : selected.value?.descriptor?.node_id
  if (!nodeId) return null
  return (props.explanationSnapshot?.nodes || []).find((node) => {
    const key = String(node.node_key || '')
    return key === nodeId || key.endsWith(`::${nodeId}`)
  }) || null
})

function identityOf(child) {
  if (child.type === 'func') return `f:${child.id}`
  if (child.type === 'cross') return `x:${child.target_service}:${child.target_function}`
  return `e:${child.id}`
}

function ancestryIds(node) {
  const ids = new Set()
  let cur = node
  while (cur) {
    if (cur.data && cur.data.identity) ids.add(cur.data.identity)
    cur = cur.parent
  }
  return ids
}

function paramsFor(data) {
  if (data.type === 'func') return { snapshot_id: props.snapshotId, node_id: data.id }
  if (data.type === 'cross') return { repo: data.target_service, handler: data.target_function }
  return null
}

function buildNodes(payload, ancestors) {
  const kids = (payload && payload.children) || []
  const nodes = kids.map((child) => {
    const identity = identityOf(child)
    const isCycle = ancestors.has(identity)
    return {
      ...child,
      __key: `n${++seq}`,
      identity,
      cycle: isCycle,
      expandable: isCycle ? false : child.expandable,
      leaf: !child.expandable || isCycle,
    }
  })
  if (payload && payload.truncated) {
    nodes.push({ __key: `n${++seq}`, type: 'note', name: t('… 直接子调用较多，仅展示前 60 条'), leaf: true })
  }
  return nodes
}

async function loadNode(node, resolve) {
  // A lazy el-tree has no :data roots: Element Plus calls `load` on the
  // virtual root (level 0). We resolve the endpoint handler's direct callees
  // there, so the first level is visible immediately under the root header.
  // Every deeper level is fetched on demand when its node is expanded.
  if (node.level === 0) {
    rootError.value = ''
    try {
      const payload = await apiClient.get('/api/call-tree/children', {
        snapshot_id: props.snapshotId, node_id: props.label.node_id || props.label.handler || '',
      })
      rootLoaded.value = true
      rootEmpty.value = !payload || !(payload.children || []).length
      resolve(buildNodes(payload, new Set()))
    } catch (err) {
      rootError.value = err.message || t('加载调用链失败')
      rootLoaded.value = true
      resolve([])
    }
    return
  }

  const params = paramsFor(node.data)
  if (params && props.snapshotId) params.snapshot_id = props.snapshotId
  if (!params) {
    resolve([])
    return
  }
  try {
    const payload = await apiClient.get('/api/call-tree/children', params)
    resolve(buildNodes(payload, ancestryIds(node)))
  } catch {
    resolve([])
  }
}

// ── selection → right rail ────────────────────────────────────────────────

function rowClickable(type) {
  return type === 'func' || type === 'cross' || type === 'external'
}

function selectRoot() {
  selected.value = {
    kind: 'root',
    identity: 'root',
    title: `${rootLabel.value.method} ${rootLabel.value.path}`.trim(),
    subtitle: rootLabel.value.handler,
    descriptor: { repo: props.repo },
    rootMeta: {
      method: rootLabel.value.method,
      path: rootLabel.value.path,
      handler: rootLabel.value.handler,
      node_id: props.label.node_id || '',
    },
  }
}

function onNodeClick(data) {
  if (data.type === 'func') {
    selected.value = {
      kind: 'func',
      node_type: 'func',
      identity: identityOf(data),
      title: data.name || data.qualified_name || '',
      subtitle: data.file ? `${data.file}:${data.line}` : '',
      descriptor: { repo: props.repo, member: data.member || props.member, node_id: data.id },
    }
  } else if (data.type === 'cross') {
    selected.value = {
      kind: 'cross',
      node_type: 'cross',
      identity: identityOf(data),
      title: `${data.http_method || 'HTTP'} ${data.path || data.url_pattern || ''}`.trim() || data.name || '',
      subtitle: `→ ${data.target_service}::${data.target_function || data.url_pattern}`,
      descriptor: { repo: data.target_service, handler: data.target_function },
    }
  } else if (data.type === 'external') {
    selected.value = {
      kind: 'external',
      identity: identityOf(data),
      title: data.name || data.url || t('外部调用'),
      subtitle: data.url ? '' : '',
    }
  } else if (data.type === 'note') {
    selected.value = { kind: 'note', identity: `note:${data.__key}`, title: data.name || t('提示') }
  }
}

function dividerLabel(index) {
  return index === 0 ? t('调整节点解释与源码宽度') : ''
}

function resizeDivider(index, delta) {
  const base = columnWidths.value
  const left = index
  const right = index + 1
  const total = base[left] + base[right]
  const nextLeft = Math.max(20, Math.min(total - 20, base[left] + delta))
  const applied = nextLeft - base[left]
  columnWidths.value = base.map((value, position) => {
    if (position === left) return nextLeft
    if (position === right) return value - applied
    return value
  })
}

function startResize(index, event) {
  if (event.button !== undefined && event.button !== 0) return
  const element = splitRef.value
  if (!element) return
  resizeState = {
    index,
    startX: event.clientX,
    width: element.getBoundingClientRect().width,
    widths: [...columnWidths.value],
  }
  window.addEventListener('pointermove', onResize)
  window.addEventListener('pointerup', stopResize)
  event.currentTarget?.setPointerCapture?.(event.pointerId)
}

function onResize(event) {
  if (!resizeState || !resizeState.width) return
  const delta = ((event.clientX - resizeState.startX) / resizeState.width) * 100
  const base = resizeState.widths
  const index = resizeState.index
  const left = index
  const right = index + 1
  const total = base[left] + base[right]
  const nextLeft = Math.max(20, Math.min(total - 20, base[left] + delta))
  const applied = nextLeft - base[left]
  columnWidths.value = base.map((value, position) => {
    if (position === left) return nextLeft
    if (position === right) return value - applied
    return value
  })
}

function stopResize() {
  resizeState = null
  window.removeEventListener('pointermove', onResize)
  window.removeEventListener('pointerup', stopResize)
}

function resetWidths() {
  stopResize()
  columnWidths.value = [55, 45]
}

onUnmounted(stopResize)
</script>

<style scoped>
.call-chain-tree { margin-bottom: 14px; }
.ct-layout { display: grid; gap: 12px; min-width: 0; }
.ct-column { min-width: 0; max-height: 480px; overflow: auto; }
.ct-call-chain { padding-bottom: 2px; }
.ct-details { display: grid; align-items: stretch; min-width: 0; }
.ct-row-divider { border-top: 1px solid #ececf2; }
.ct-column-title { color: #a0a6b1; font-size: 11px; margin-bottom: 6px; }
.ct-divider { width: 10px; min-height: 100%; cursor: col-resize; position: relative; touch-action: none; }
.ct-divider::before { content: ''; position: absolute; top: 0; bottom: 0; left: 4px; border-left: 1px solid #e2e5eb; }
.ct-divider:hover::before, .ct-divider:focus::before { border-color: #e94560; border-left-width: 2px; }
.ct-root { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 4px 6px 6px; border-bottom: 1px dashed #e2e5eb; margin-bottom: 2px; font-size: 12px; cursor: pointer; border-radius: 4px; }
.ct-root:hover { background: #faf7ff; }
.ct-root-selected { background: #fff0f2; }
.ct-tree { background: transparent; font-size: 12px; }
.ct-row { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }
.ct-row code { font-size: 11px; }
.ct-row-clickable { cursor: pointer; }
.ct-badge { display: inline-block; min-width: 34px; text-align: center; font-size: 10px; font-weight: 700; color: #fff; background: #e94560; border-radius: 4px; padding: 1px 5px; flex-shrink: 0; }
.ct-badge-method { background: #2a6496; }
.ct-badge-ext { background: #8a94a6; }
.ct-path { color: #c82d48; font-weight: 600; }
.ct-name { color: #333; }
.ct-fn-dot { width: 7px; height: 7px; border-radius: 50%; background: #7c5cf0; display: inline-block; flex-shrink: 0; }
.ct-arrow { color: #e94560; font-weight: 700; }
.ct-svc { color: #2a6496; }
.ct-dim { color: #a0a6b1; font-size: 11px; }
.ct-note { color: #b98a00; font-size: 11px; background: #fff8e1; border-radius: 4px; padding: 0 5px; }
.ct-cycle { color: #b98a00; font-size: 10px; background: #fff8e1; border-radius: 4px; padding: 0 4px; }
.ct-error { color: #c0392b; font-size: 11px; }
.ct-rail { padding: 0 10px; }
.ct-source-rail { padding-left: 10px; }
:deep(.el-tree-node__content) { height: 26px; }
:deep(.el-tree-node__content:hover) { background: #fff0f2; }
:deep(.el-tree-node.is-current > .el-tree-node__content) { background: #fff0f2; }
@media (max-width: 900px) {
  .ct-details { display: block; }
  .ct-column { max-height: none; overflow: visible; padding: 0 0 12px; }
  .ct-divider { display: none; }
}
</style>
