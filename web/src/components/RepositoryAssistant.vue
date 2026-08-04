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
        <button :class="{ active: tab === 'ui-tests' }" @click="showUiTests">UI 测试</button>
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

      <section v-else-if="tab === 'audit'" class="audit-body">
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

      <section v-else class="ui-test-body">
        <div class="recorder-status" :class="{ recording: activeRecording }">
          {{ activeRecording ? `正在录制 #${activeRecording.id}，请在新标签页操作` : '当前未录制' }}
        </div>
        <form v-if="!activeRecording" class="target-form" @submit.prevent="startRecording">
          <label>测试名称<input v-model.trim="testName" required placeholder="例如：创建商品" /></label>
          <label>目标名称<input v-model.trim="targetName" required placeholder="例如：mall-admin" /></label>
          <label>目标地址<input v-model.trim="targetUrl" required type="url" placeholder="http://localhost:8080" /></label>
          <button :disabled="uiBusy">{{ uiBusy ? '正在打开...' : '打开并开始录制' }}</button>
        </form>
        <div v-else class="record-actions">
          <button @click="collectRecording" :disabled="uiBusy">同步步骤</button>
          <button class="danger" @click="stopRecording" :disabled="uiBusy">停止录制</button>
        </div>
        <p v-if="uiError" class="error">{{ uiError }}</p>
        <div class="recordings-heading"><b>录制记录</b><button @click="loadRecordings">刷新</button></div>
        <article v-for="recording in recordings" :key="recording.id" class="recording-entry">
          <div><b>{{ recording.name }}</b><span>#{{ recording.id }} · {{ recording.status }}</span></div>
          <small>{{ recording.start_url }}</small>
          <p>{{ recording.steps?.length || 0 }} 步 · {{ recording.network_log?.length || 0 }} 个接口请求</p>
          <details v-if="recording.steps?.length"><summary>查看 DSL</summary><pre>{{ format(recording.steps) }}</pre></details>
          <button v-if="recording.status === 'recorded'" @click="runRecording(recording)" :disabled="uiBusy">回放</button>
          <span v-if="recording.lastRun" :class="['run-result', recording.lastRun.status]">
            {{ recording.lastRun.status }}<template v-if="recording.lastRun.error">：{{ recording.lastRun.error }}</template>
          </span>
        </article>
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
  data: () => ({
    open: false, tab: 'chat', question: '', sending: false, auditLoading: false,
    messages: [], logs: [], targets: [], recordings: [], activeRecording: null,
    targetName: '', targetUrl: '', testName: '', uiBusy: false, uiError: '', collectTimer: null,
  }),
  beforeUnmount() { this.stopCollectTimer() },
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
    async showUiTests() {
      this.tab = 'ui-tests'
      await Promise.all([this.loadTargets(), this.loadRecordings()])
    },
    async loadTargets() {
      const response = await this.$api.get('/api/ui-test-targets', { repo: this.repoName })
      this.targets = response.targets || []
    },
    async loadRecordings() {
      const response = await this.$api.get('/api/ui-recordings', { repo: this.repoName })
      this.recordings = response.recordings || []
      this.activeRecording = this.recordings.find(item => item.status === 'recording') || null
      if (this.activeRecording) this.startCollectTimer()
    },
    async startRecording() {
      this.uiBusy = true; this.uiError = ''
      try {
        const target = await this.$api.request('/api/ui-test-targets', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo: this.repoName, name: this.targetName, base_url: this.targetUrl, allowed_origins: [] }),
        })
        const existing = this.targets.findIndex(item => item.id === target.id)
        if (existing >= 0) this.targets.splice(existing, 1, target); else this.targets.push(target)
        this.activeRecording = await this.$api.request('/api/ui-recordings/start', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo: this.repoName, target_id: target.id, name: this.testName, start_url: this.targetUrl }),
        })
        this.startCollectTimer()
        await this.loadRecordings()
      } catch (error) { this.uiError = error.message || '无法开始录制' }
      finally { this.uiBusy = false }
    },
    startCollectTimer() {
      this.stopCollectTimer()
      this.collectTimer = setInterval(() => this.collectRecording(), 1000)
    },
    stopCollectTimer() { if (this.collectTimer) clearInterval(this.collectTimer); this.collectTimer = null },
    async collectRecording() {
      if (!this.activeRecording || this.uiBusy) return
      try {
        this.activeRecording = await this.$api.request(`/api/ui-recordings/${this.activeRecording.id}/collect`, { method: 'POST' })
      } catch (error) { this.uiError = error.message || '同步录制步骤失败' }
    },
    async stopRecording() {
      if (!this.activeRecording) return
      this.uiBusy = true; this.stopCollectTimer()
      try {
        await this.$api.request(`/api/ui-recordings/${this.activeRecording.id}/stop`, { method: 'POST' })
        this.activeRecording = null
        await this.loadRecordings()
      } catch (error) { this.uiError = error.message || '停止录制失败' }
      finally { this.uiBusy = false }
    },
    async runRecording(recording) {
      this.uiBusy = true; this.uiError = ''
      try {
        recording.lastRun = await this.$api.request(`/api/ui-recordings/${recording.id}/run`, { method: 'POST' })
      } catch (error) { this.uiError = error.message || '回放失败' }
      finally { this.uiBusy = false }
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
.chat-body, .audit-body, .ui-test-body { flex: 1; overflow: auto; padding: 16px; }
.welcome { color: #777; background: #f6f7fa; border-radius: 8px; padding: 14px; line-height: 1.6; }
.message { margin: 10px 0; padding: 11px 13px; border-radius: 10px; line-height: 1.5; } .message.user { margin-left: 45px; background: #fff0f2; } .message.assistant { margin-right: 25px; background: #f3f5f8; } .message.error { color: #a82038; }
.message p, .audit-entry p { margin: 0 0 7px; white-space: pre-wrap; } details summary { cursor: pointer; color: #555; font-size: 12px; }
.operation { margin-top: 8px; } .operation > span { float: right; color: #888; font-size: 11px; } code { color: #c52d47; font-size: 11px; } pre { max-height: 180px; overflow: auto; background: #202333; color: #d9dfef; padding: 8px; font-size: 10px; white-space: pre-wrap; }
.composer { border-top: 1px solid #ddd; padding: 12px; display: flex; gap: 8px; } textarea { flex: 1; min-height: 62px; resize: vertical; border: 1px solid #ccc; border-radius: 6px; padding: 8px; } .composer button { border: 0; border-radius: 6px; padding: 0 16px; color: white; background: #e94560; } .composer button:disabled { opacity: .5; }
.audit-entry { padding: 11px 0; border-bottom: 1px solid #eee; } .audit-entry > div { display: flex; justify-content: space-between; } .audit-entry time, .audit-entry small { color: #888; font-size: 11px; } .audit-entry code { display: inline-block; margin: 3px 5px 5px 0; background: #fff0f2; padding: 2px 5px; } .error { color: #a82038; }
.recorder-status { padding: 10px; border-radius: 6px; background: #f3f5f8; margin-bottom: 12px; } .recorder-status.recording { background: #fff0f2; color: #c52d47; }
.target-form { display: grid; gap: 9px; padding-bottom: 14px; border-bottom: 1px solid #eee; } .target-form label { display: grid; gap: 4px; font-size: 12px; color: #666; } .target-form input { padding: 8px; border: 1px solid #ccc; border-radius: 5px; } .target-form button, .record-actions button, .recording-entry button { border: 0; border-radius: 5px; padding: 7px 11px; background: #e94560; color: #fff; cursor: pointer; }
.record-actions { display: flex; gap: 8px; margin-bottom: 12px; } .record-actions .danger { background: #a82038; }
.recordings-heading { display: flex; justify-content: space-between; margin: 14px 0 6px; } .recordings-heading button { border: 0; background: none; color: #e94560; cursor: pointer; }
.recording-entry { padding: 11px 0; border-bottom: 1px solid #eee; } .recording-entry > div { display: flex; justify-content: space-between; gap: 8px; } .recording-entry span, .recording-entry small { color: #888; font-size: 11px; } .recording-entry p { margin: 5px 0; font-size: 12px; } .recording-entry pre { max-height: 160px; } .run-result { margin-left: 8px; } .run-result.passed { color: #16834b; } .run-result.failed { color: #a82038; }
</style>
