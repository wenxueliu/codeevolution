<template>
  <div v-if="repoName" class="assistant-shell">
    <button class="assistant-toggle" @click="open = !open" :aria-expanded="open">{{ open ? '关闭问答' : '代码问答' }}</button>
    <aside v-if="open" class="assistant-panel" aria-label="代码仓问答助手">
      <header>
        <div><strong>代码仓问答</strong><small>{{ repoName }} · 只读操作</small></div>
        <button @click="open = false" aria-label="关闭">×</button>
      </header>
      <div class="tabs">
        <button :class="{ active: tab === 'chat' }" @click="tab = 'chat'">问答</button>
        <button :class="{ active: tab === 'audit' }" @click="showAudit">审计日志</button>
      </div>

      <section v-if="tab === 'chat'" class="chat-body">
        <div class="welcome" v-if="!messages.length">询问类、方法、调用方、功能历史或演进统计。模型会生成受控查询计划，不执行任意 SQL。</div>
        <article v-for="(message, index) in messages" :key="index" :class="['message', message.role]">
          <p>{{ message.text }}</p>
          <details v-if="message.operations?.length">
            <summary>查看执行操作（{{ message.operations.length }}）</summary>
            <div v-for="operation in message.operations" :key="operation.operation" class="operation">
              <code>{{ operation.operation }}</code><span>{{ operation.source }}</span>
              <pre>{{ format(operation.rows) }}</pre>
            </div>
          </details>
        </article>
      </section>

      <section v-else class="audit-body">
        <div v-if="auditLoading">正在加载...</div>
        <article v-for="log in logs" :key="log.id" class="audit-entry">
          <div><b>#{{ log.id }} {{ log.status }}</b><time>{{ formatTime(log.created_at) }}</time></div>
          <p>{{ log.question }}</p>
          <code v-for="item in log.plan" :key="item.operation">{{ item.operation }}</code>
          <small>{{ log.result_count }} 条 · {{ Number(log.duration_ms).toFixed(1) }} ms</small>
          <p v-if="log.error" class="error">{{ log.error }}</p>
        </article>
        <div v-if="!auditLoading && !logs.length" class="welcome">暂无审计记录</div>
      </section>

      <form v-if="tab === 'chat'" @submit.prevent="send" class="composer">
        <textarea v-model.trim="question" :disabled="sending" maxlength="2000" placeholder="例如：谁调用了 createOrder？" @keydown.ctrl.enter="send"></textarea>
        <button :disabled="sending || !question">{{ sending ? '查询中...' : '发送' }}</button>
      </form>
    </aside>
  </div>
</template>

<script>
export default {
  name: 'RepositoryAssistant',
  props: { repoName: { type: String, default: '' } },
  data: () => ({ open: false, tab: 'chat', question: '', sending: false, auditLoading: false, messages: [], logs: [] }),
  methods: {
    async send() {
      if (!this.question || this.sending) return
      const question = this.question
      this.messages.push({ role: 'user', text: question })
      this.question = ''
      this.sending = true
      try {
        const response = await this.$api.request('/api/chat', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo: this.repoName, question }),
        })
        this.messages.push({ role: 'assistant', text: response.answer, operations: response.operations })
      } catch (error) {
        this.messages.push({ role: 'assistant error', text: error.message || '问答请求失败' })
      } finally { this.sending = false }
    },
    async showAudit() {
      this.tab = 'audit'; this.auditLoading = true
      try {
        const response = await this.$api.get('/api/audit-logs', { repo: this.repoName, limit: 50 })
        this.logs = response.logs || []
      } finally { this.auditLoading = false }
    },
    format(value) { return JSON.stringify(value, null, 2) },
    formatTime(value) { return new Date(value * 1000).toLocaleString() },
  },
}
</script>

<style scoped>
.assistant-toggle { position: fixed; right: 0; top: 42%; z-index: 30; border: 0; border-radius: 8px 0 0 8px; background: #e94560; color: white; padding: 12px 9px; writing-mode: vertical-rl; cursor: pointer; box-shadow: 0 2px 12px #0003; }
.assistant-panel { position: fixed; z-index: 29; right: 0; top: 52px; bottom: 0; width: min(440px, 94vw); background: #fff; box-shadow: -4px 0 20px #0002; display: flex; flex-direction: column; }
header { padding: 16px 18px; background: #1a1a2e; color: #fff; display: flex; justify-content: space-between; align-items: center; }
header div { display: flex; flex-direction: column; gap: 3px; } header small { color: #aaa; } header button { border: 0; background: none; color: #fff; font-size: 24px; cursor: pointer; }
.tabs { display: flex; border-bottom: 1px solid #eee; } .tabs button { flex: 1; padding: 11px; border: 0; background: #fff; cursor: pointer; } .tabs .active { color: #e94560; border-bottom: 2px solid #e94560; }
.chat-body, .audit-body { flex: 1; overflow: auto; padding: 16px; }
.welcome { color: #777; background: #f6f7fa; border-radius: 8px; padding: 14px; line-height: 1.6; }
.message { margin: 10px 0; padding: 11px 13px; border-radius: 10px; line-height: 1.5; } .message.user { margin-left: 45px; background: #fff0f2; } .message.assistant { margin-right: 25px; background: #f3f5f8; } .message.error { color: #a82038; }
.message p, .audit-entry p { margin: 0 0 7px; white-space: pre-wrap; } details summary { cursor: pointer; color: #555; font-size: 12px; }
.operation { margin-top: 8px; } .operation > span { float: right; color: #888; font-size: 11px; } code { color: #c52d47; font-size: 11px; } pre { max-height: 180px; overflow: auto; background: #202333; color: #d9dfef; padding: 8px; font-size: 10px; white-space: pre-wrap; }
.composer { border-top: 1px solid #ddd; padding: 12px; display: flex; gap: 8px; } textarea { flex: 1; min-height: 62px; resize: vertical; border: 1px solid #ccc; border-radius: 6px; padding: 8px; } .composer button { border: 0; border-radius: 6px; padding: 0 16px; color: white; background: #e94560; } .composer button:disabled { opacity: .5; }
.audit-entry { padding: 11px 0; border-bottom: 1px solid #eee; } .audit-entry > div { display: flex; justify-content: space-between; } .audit-entry time, .audit-entry small { color: #888; font-size: 11px; } .audit-entry code { display: inline-block; margin: 3px 5px 5px 0; background: #fff0f2; padding: 2px 5px; } .error { color: #a82038; }
</style>
