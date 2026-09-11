<template>
  <div class="topology-graph" :aria-label="ariaLabel" role="img">
    <el-empty v-if="!graph?.nodes?.length" :description="emptyText" :image-size="72" />
    <svg v-else :viewBox="`0 0 ${graph.width} ${graph.height}`" preserveAspectRatio="xMinYMin meet">
      <defs>
        <marker :id="markerId" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" />
        </marker>
      </defs>
      <g class="graph-edges">
        <line
          v-for="edge in graph.edges"
          :key="edge.id"
          :class="`graph-edge edge-${edge.kind || 'default'}`"
          :x1="edge.x1"
          :y1="edge.y1"
          :x2="edge.x2"
          :y2="edge.y2"
          :marker-end="`url(#${markerId})`"
        />
      </g>
      <g
        v-for="node in graph.nodes"
        :key="node.id"
        class="graph-node"
        :class="`node-${node.kind || 'service'}`"
        :transform="`translate(${node.x} ${node.y})`"
      >
        <rect :width="node.width" :height="node.height" rx="8" />
        <text class="node-label" :x="node.width / 2" y="23" text-anchor="middle">{{ node.label }}</text>
        <text v-if="node.meta" class="node-meta" :x="node.width / 2" y="42" text-anchor="middle">{{ node.meta }}</text>
      </g>
    </svg>
  </div>
</template>

<script>
export default {
  props: {
    graph: { type: Object, default: () => ({ nodes: [], edges: [], width: 760, height: 180 }) },
    ariaLabel: { type: String, default: '' },
    emptyText: { type: String, default: 'No graph data' },
  },
  computed: {
    markerId() {
      return `topology-arrow-${this._.uid}`
    },
  },
}
</script>

<style scoped>
.topology-graph { min-height: 150px; overflow-x: auto; padding: 8px 0; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; }
svg { display: block; min-width: 680px; width: 100%; height: auto; max-height: 460px; }
marker path { fill: #94a3b8; }
.graph-edge { stroke: #94a3b8; stroke-width: 1.8; }
.edge-message { stroke: #8b5cf6; stroke-dasharray: 5 3; }
.edge-grpc { stroke: #0ea5e9; }
.edge-resource { stroke: #f59e0b; stroke-dasharray: 4 3; }
.graph-node rect { fill: #fff; stroke: #315d9b; stroke-width: 1.5; filter: drop-shadow(0 2px 3px rgba(15, 23, 42, .08)); }
.node-resource rect { fill: #fff7ed; stroke: #f59e0b; }
.node-unknown rect { fill: #f8fafc; stroke: #94a3b8; stroke-dasharray: 4 3; }
.node-label { fill: #1f2937; font-size: 12px; font-weight: 650; }
.node-meta { fill: #64748b; font-size: 10px; }
</style>
