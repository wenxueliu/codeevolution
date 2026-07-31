<template>
  <div class="feature-detail" v-if="feature">
    <div v-if="error" class="request-error">{{ error.message }}</div>
    <div class="back-row">
      <router-link :to="'/repo/' + repoName + '/features'" class="back-link">&larr; 返回功能列表</router-link>
    </div>

    <div class="detail-header">
      <h1>{{ feature.canonical_name }}</h1>
      <div class="descriptions" v-if="feature.description || feature.description_zh">
        <div class="desc-zh" v-if="feature.description_zh">{{ feature.description_zh }}</div>
        <div class="desc-en" v-if="feature.description">{{ feature.description }}</div>
      </div>
      <div class="meta">
        <span class="type-tag">{{ feature.entry_type }}</span>
        <span class="status-tag" :class="feature.status">{{ feature.status }}</span>
        <span>{{ feature.event_count }} 个事件</span>
        <span v-if="feature.call_chain?.length">{{ feature.call_chain.length }} 步调用</span>
      </div>
      <div class="stable-id">{{ feature.stable_id }}</div>
    </div>

    <!-- AI Explanation (optional, requires LLM config) -->
    <div class="explain-section" v-if="llmAvailable || explanation">
      <div class="explain-header">
        <h3>AI 实现解释</h3>
        <button v-if="!explanation && !explainLoading" @click="loadExplanation" class="explain-btn">
          生成解释
        </button>
        <span v-if="explainLoading" class="explain-loading">生成中...</span>
      </div>
      <div v-if="explanation" class="explain-body">
        <div class="explain-zh" v-if="explanation.zh">{{ explanation.zh }}</div>
        <div class="explain-en" v-if="explanation.en">{{ explanation.en }}</div>
      </div>
      <div v-if="explainError" class="explain-error">{{ explainError }}</div>
    </div>

    <!-- Mermaid Sequence Diagram -->
    <div class="sequence-section" v-if="feature.call_chain?.length">
      <h3>调用时序图</h3>
      <div v-if="diagramError" class="mermaid-error">时序图渲染失败</div>
      <div class="mermaid-wrap" v-show="!diagramError" ref="wrapEl">
        <pre class="mermaid" ref="mermaidEl">{{ mermaidText }}</pre>
      </div>
    </div>
    <div class="no-sequence" v-else>
      <p>该功能无内部调用链（未检测到同一文件内的函数调用）</p>
    </div>

    <!-- Timeline -->
    <div class="timeline-section">
      <h3>演变时间线</h3>
      <div class="timeline" v-if="timeline.length">
        <div v-for="(ev, idx) in timeline" :key="idx" class="timeline-item">
          <div class="tl-dot" :class="ev.event_type"></div>
          <div class="tl-content">
            <div class="tl-header">
              <span class="event-badge" :class="ev.event_type">{{ ev.event_type }}</span>
              <span class="tl-date">{{ formatDate(ev.timestamp) }}</span>
              <span class="tl-commit">{{ ev.commit_hash?.slice(0, 7) }}</span>
            </div>
            <div class="tl-author">{{ ev.author }}</div>
            <div class="tl-message">{{ ev.message }}</div>
            <div v-if="ev.detail" class="tl-detail">
              <code>{{ formatDetail(ev.detail) }}</code>
            </div>
          </div>
        </div>
      </div>
      <p v-else class="empty">暂无时间线数据</p>
    </div>
  </div>
  <div v-else-if="error" class="request-error">{{ error.message }}</div>
  <div v-else class="loading">加载中...</div>
</template>

<script>
import mermaid from 'mermaid'

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  sequence: {
    useMaxWidth: true,
    wrap: true,
  },
})

// Extract class and method from qualified name
// "GraphStore.get_subgraph" → {cls:"GraphStore", method:"get_subgraph"}
// "self.get_node" → null (self-call, use caller's class)
function parseCall(fullName) {
  if (!fullName) return { cls: null, method: fullName }
  const stripped = fullName.replace(/^self\./, '')
  const dotIdx = stripped.lastIndexOf('.')
  if (dotIdx > 0 && dotIdx < stripped.length - 1) {
    return { cls: stripped.substring(0, dotIdx), method: stripped.substring(dotIdx + 1) }
  }
  return { cls: null, method: stripped }
}

export default {
  props: { stableId: String, repoName: String },
  data() {
    return { feature: null, diagramError: false, llmAvailable: false, explanation: null, explainLoading: false, explainError: null }
  },
  computed: {
    timeline() { return this.feature?.timeline || [] },
    mermaidText() {
      const chain = this.feature?.call_chain || []
      if (!chain.length) return ''
      const m = (n) => { const p = n.replace(/^self\./, '').split('.'); return p[p.length-1] }

      // Determine the class for each function: parse qualified name or self-call
      const entryCls = parseCall(chain[0].from).cls || 'EntryPoint'
      const fnClass = {}  // qualifiedName → className

      function resolveClass(fullName, callerClass) {
        if (fnClass[fullName]) return fnClass[fullName]
        if (fullName.startsWith('self.')) {
          return callerClass || entryCls
        }
        const p = parseCall(fullName)
        return p.cls || callerClass || entryCls
      }

      // Build class mapping
      fnClass[chain[0].from] = entryCls
      for (const c of chain) {
        // Ensure caller's class is known
        if (!fnClass[c.from]) {
          fnClass[c.from] = resolveClass(c.from, entryCls)
        }
        // Determine callee's class
        fnClass[c.to] = resolveClass(c.to, fnClass[c.from])
      }

      // Collect unique classes as participants
      const classes = []
      const clsSeen = new Set()
      for (const name of Object.values(fnClass)) {
        if (!clsSeen.has(name)) { clsSeen.add(name); classes.push(name) }
      }

      // Map class name → alias
      const clsAlias = {}
      classes.forEach((c, i) => { clsAlias[c] = 'C' + i })

      // Build mermaid DSL
      let lines = ['sequenceDiagram']
      for (const cls of classes) {
        lines.push('  participant ' + clsAlias[cls] + ' as ' + cls)
      }

      for (const c of chain) {
        const fromCls = fnClass[c.from] || entryCls
        const toCls = fnClass[c.to] || entryCls
        const fromA = clsAlias[fromCls]
        const toA = clsAlias[toCls]
        const methodName = m(c.to)
        if (fromA === toA) {
          // Self-call
          lines.push('  ' + fromA + '->>+' + fromA + ': ' + methodName + '()')
        } else {
          lines.push('  ' + fromA + '->>+' + toA + ': ' + methodName + '()')
        }
      }

      // Return arrows (deactivation)
      const rev = [...chain].reverse()
      const retSeen = new Set()
      for (const c of rev) {
        const toCls = fnClass[c.to] || entryCls
        const toA = clsAlias[toCls]
        if (!retSeen.has(toA + '_' + c.depth)) {
          retSeen.add(toA + '_' + c.depth)
          const fromCls = fnClass[c.from] || entryCls
          lines.push('  ' + toA + '-->>-' + clsAlias[fromCls] + ': ')
        }
      }

      return lines.join('\n')
    },
  },
  watch: {
    mermaidText(val) {
      if (val) {
        this.$nextTick(() => { setTimeout(() => this.renderDiagram(), 50) })
      }
    },
  },
  created() { this.loadFeature() },
  methods: {
    async loadFeature() {
      await this.$runAsync(async () => {
        this.feature = await this.$api.get('/api/features/' + encodeURIComponent(this.stableId), {
          repo: this.repoName || '',
        })
        // Check if LLM is available
        await this.checkLLM()
      })
    },
    async checkLLM() {
      await this.$runAsync(async () => {
        const d = await this.$api.get('/api/llm-status')
        this.llmAvailable = d.available
      })
    },
    async loadExplanation() {
      this.explainLoading = true
      this.explainError = null
      try {
        const d = await this.$api.get('/api/features/' + encodeURIComponent(this.stableId) + '/explain', {
          repo: this.repoName || '',
        })
        if (d.available && d.explanation) {
          this.explanation = d.explanation
        } else if (!d.available) {
          this.explainError = '需要配置 OPENAI_API_KEY 环境变量'
        } else {
          this.explainError = '生成失败'
        }
      } catch(e) {
        this.explainError = '请求失败: ' + e.message
      } finally {
        this.explainLoading = false
      }
    },
    async renderDiagram() {
      if (!this.$refs.mermaidEl) return
      this.diagramError = false
      try {
        await mermaid.run({ nodes: [this.$refs.mermaidEl] })
      } catch(e) {
        console.error('Mermaid error:', e)
        this.diagramError = true
      }
    },
    formatDate(ts) { return new Date(ts * 1000).toLocaleDateString('zh-CN') },
    formatDetail(d) {
      if (typeof d === 'string') {
        try { d = JSON.parse(d) } catch(e) { return d }
      }
      return Object.entries(d).map(([k,v]) => k + ': ' + v).join(', ')
    },
  },
}
</script>

<style scoped>
.back-row { margin-bottom: 16px; }
.back-link { color: #888; text-decoration: none; font-size: 13px; }
.back-link:hover { color: #e94560; }
.detail-header { background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.detail-header h1 { font-size: 22px; margin-bottom: 8px; word-break: break-all; }
.descriptions { margin-bottom: 12px; }
.desc-zh { font-size: 14px; color: #333; margin-bottom: 2px; font-weight: 500; }
.desc-en { font-size: 12px; color: #888; }
.meta { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; font-size: 14px; color: #666; flex-wrap: wrap; }
.type-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; background: #e8e8e8; color: #555; }
.status-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.status-tag.active { background: #d4edda; color: #155724; }
.status-tag.removed { background: #f8d7da; color: #721c24; }
.stable-id { font-family: monospace; font-size: 12px; color: #aaa; }

/* Explain */
.explain-section { background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.explain-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.explain-header h3 { font-size: 16px; }
.explain-btn { padding: 6px 16px; background: #e94560; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; }
.explain-btn:hover { background: #c73e54; }
.explain-loading { font-size: 13px; color: #ffc107; }
.explain-body { font-size: 14px; line-height: 1.8; }
.explain-zh { color: #333; margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px solid #f0f0f0; }
.explain-en { color: #666; font-size: 13px; }
.explain-error { color: #dc3545; font-size: 13px; }
/* Placeholder for LLM unavailable */
.no-explain { background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; color: #999; font-size: 13px; }

/* Mermaid */
.sequence-section { background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow-x: auto; }
.sequence-section h3 { font-size: 16px; margin-bottom: 16px; }
.mermaid-wrap { min-height: 100px; display: flex; justify-content: center; }
.mermaid-wrap :deep(svg) { max-width: 100%; height: auto; }
.mermaid-error { text-align: center; color: #dc3545; padding: 20px; }
.no-sequence {background: #fff; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; color: #999; font-size: 14px; }

/* Timeline */
.timeline-section { background: #fff; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.timeline-section h3 { font-size: 16px; margin-bottom: 20px; }
.timeline { position: relative; padding-left: 24px; border-left: 2px solid #eee; margin-left: 8px; }
.timeline-item { position: relative; margin-bottom: 20px; }
.tl-dot { position: absolute; left: -33px; top: 4px; width: 12px; height: 12px; border-radius: 50%; background: #ccc; border: 2px solid #fff; }
.tl-dot.BORN { background: #28a745; }
.tl-dot.DIED { background: #dc3545; }
.tl-dot.GROWN, .tl-dot.EXTENDED, .tl-dot.DEP_CREATED { background: #ffc107; }
.tl-dot.SHRUNK, .tl-dot.CONTRACTED, .tl-dot.DEP_REMOVED { background: #fd7e14; }
.tl-dot.MOVED { background: #6f42c1; }
.tl-dot.MODIFIED { background: #17a2b8; }
.tl-dot.UNCHANGED { background: #ccc; }
.tl-content { padding-left: 8px; }
.tl-header { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.event-badge { padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; background: #eee; color: #555; }
.event-badge.BORN { background: #d4edda; color: #155724; }
.event-badge.DIED { background: #f8d7da; color: #721c24; }
.event-badge.GROWN, .event-badge.EXTENDED, .event-badge.DEP_CREATED { background: #fff3cd; color: #856404; }
.event-badge.SHRUNK, .event-badge.CONTRACTED, .event-badge.DEP_REMOVED { background: #ffe5cc; color: #a0401a; }
.event-badge.MOVED { background: #e8d5f5; color: #5a2d8a; }
.tl-date { font-size: 12px; color: #999; }
.tl-commit { font-family: monospace; font-size: 11px; color: #bbb; }
.tl-author { font-size: 13px; color: #666; margin-bottom: 2px; }
.tl-message { font-size: 13px; color: #444; margin-bottom: 4px; }
.tl-detail code { font-size: 11px; color: #888; background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }
.loading { text-align: center; padding: 60px; color: #999; }
.empty { color: #999; text-align: center; padding: 40px 0; font-size: 14px; }
</style>
