<template>
  <div class="call-chain-tree">
    <div v-if="rootError" class="ct-error">{{ rootError }}</div>

    <div v-else class="ct-split">
      <!-- 左侧：端点根 + 调用链树 -->
      <div class="ct-main">
        <div
          class="ct-root"
          :class="{ 'ct-root-selected': selected?.kind === 'root' }"
          role="button"
          tabindex="0"
          title="选中端点根，在右侧查看/生成其整体业务规则"
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
                <span v-if="data.cycle" class="ct-cycle">↺ 回环</span>
                <span class="ct-dim" v-if="data.file">{{ data.file }}:{{ data.line }}</span>
              </template>

              <template v-else-if="data.type === 'cross'">
                <span class="ct-badge">{{ data.http_method || 'HTTP' }}</span>
                <code class="ct-path">{{ data.path }}</code>
                <span class="ct-arrow">→</span>
                <b class="ct-svc">{{ data.target_service }}</b>
                <span class="ct-dim">::{{ data.target_function || data.url_pattern }}</span>
                <span v-if="data.cycle" class="ct-cycle">↺ 回环</span>
              </template>

              <template v-else-if="data.type === 'external'">
                <span class="ct-badge ct-badge-ext">{{ data.http_method || 'HTTP' }}</span>
                <code class="ct-name">{{ data.name }}</code>
                <span class="ct-dim" v-if="data.url">{{ data.url }}</span>
                <span class="ct-note">{{ data.note || '外部 / 未匹配' }}</span>
              </template>

              <span v-else-if="data.type === 'note'" class="ct-note">{{ data.name }}</span>
            </span>
          </template>
        </el-tree>
      </div>

      <!-- 右侧：当前节点的业务规则解释 -->
      <aside class="ct-rail">
        <div class="ct-rail-title">后端调用链 · 节点解释与聚合</div>
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
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { apiClient } from '../api/apiClient.js'
import NodeRuleRail from './NodeRuleRail.vue'

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

const rootLabel = computed(() => ({
  method: props.label.method || 'HTTP',
  path: props.label.path || '',
  handler: props.label.handler || '',
}))

const emptyText = computed(() => {
  if (!rootLoaded.value) return '正在加载调用链…'
  return rootEmpty.value ? '该处理函数未识别到直接子调用。' : ''
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
    nodes.push({ __key: `n${++seq}`, type: 'note', name: '… 直接子调用较多，仅展示前 60 条', leaf: true })
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
      rootError.value = err.message || '加载调用链失败'
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
      title: data.name || data.url || '外部调用',
      subtitle: data.url ? '' : '',
    }
  } else if (data.type === 'note') {
    selected.value = { kind: 'note', identity: `note:${data.__key}`, title: data.name || '提示' }
  }
}
</script>

<style scoped>
.call-chain-tree { margin-bottom: 14px; }
.ct-split { display: flex; align-items: flex-start; gap: 12px; }
.ct-main { flex: 1; min-width: 0; }
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
.ct-rail { width: 360px; flex-shrink: 0; border-left: 1px solid #e2e5eb; padding-left: 12px; max-height: 480px; overflow: auto; }
.ct-rail-title { color: #a0a6b1; font-size: 11px; margin-bottom: 6px; }
:deep(.el-tree-node__content) { height: 26px; }
:deep(.el-tree-node__content:hover) { background: #fff0f2; }
:deep(.el-tree-node.is-current > .el-tree-node__content) { background: #fff0f2; }
</style>
