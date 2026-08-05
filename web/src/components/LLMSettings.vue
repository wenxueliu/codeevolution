<template>
  <div v-if="open" class="settings-backdrop" @click.self="$emit('close')">
    <section class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="llm-settings-title">
      <header>
        <div><h2 id="llm-settings-title">LLM 设置</h2><p>用于代码问答、AI 解释和 Phase 3 知识抽取。</p></div>
        <button type="button" aria-label="关闭 LLM 设置" @click="$emit('close')">×</button>
      </header>

      <UiState v-if="error" kind="error" title="配置操作失败" :message="error" />
      <div v-if="loading" class="loading">正在读取配置...</div>
      <form v-else @submit.prevent="save">
        <div v-if="settings.environment_override" class="environment-notice">
          当前由环境变量配置并优先生效。页面配置可以保存，但需移除环境变量并重启服务后才会启用。
        </div>
        <label>模型名称<input v-model.trim="form.model" required autocomplete="off" placeholder="例如：gpt-4o-mini、anthropic/claude-3-5-sonnet" /></label>
        <label>API Base <span>可选</span><input v-model.trim="form.api_base" type="url" autocomplete="url" placeholder="例如：https://api.openai.com/v1" /></label>
        <label>API Key <span>{{ settings.api_key_configured ? '留空则保留现有密钥' : '必填' }}</span><input v-model="form.api_key" :required="!settings.api_key_configured" type="password" autocomplete="new-password" placeholder="不会在页面中回显" /></label>
        <p class="security-note">密钥保存在 CodeHistory 数据目录，文件权限为仅当前用户可读写。</p>
        <div class="status-row">
          <span :class="['status-dot', settings.available ? 'ready' : '']"></span>
          {{ settings.available ? `已配置 · ${sourceLabel}` : '尚未配置' }}
        </div>
        <div class="actions">
          <button v-if="settings.stored_configured" type="button" class="danger" :disabled="busy" @click="clearConfig">清除页面配置</button>
          <span></span>
          <button type="button" class="secondary" :disabled="busy || !settings.available" @click="testConnection">{{ testing ? '正在测试...' : '测试连接' }}</button>
          <button type="submit" class="primary" :disabled="busy">{{ saving ? '正在保存...' : '保存配置' }}</button>
        </div>
        <p v-if="success" class="success" role="status">{{ success }}</p>
      </form>
    </section>
  </div>
</template>

<script>
import UiState from './UiState.vue'

export default {
  name: 'LLMSettings',
  components: { UiState },
  emits: ['close', 'saved'],
  props: { open: { type: Boolean, default: false } },
  data: () => ({ loading: false, saving: false, testing: false, error: '', success: '', settings: {}, form: { model: 'gpt-4o-mini', api_base: '', api_key: '' } }),
  computed: {
    busy() { return this.saving || this.testing },
    sourceLabel() { return this.settings.source === 'environment' ? '环境变量' : '页面配置' },
  },
  watch: {
    open(value) { if (value) this.load() },
  },
  mounted() { window.addEventListener('keydown', this.onKeydown); if (this.open) this.load() },
  beforeUnmount() { window.removeEventListener('keydown', this.onKeydown) },
  methods: {
    onKeydown(event) { if (event.key === 'Escape' && this.open) this.$emit('close') },
    async load() {
      this.loading = true; this.error = ''; this.success = ''
      try {
        this.settings = await this.$api.get('/api/llm-config')
        this.form = { model: this.settings.model || 'gpt-4o-mini', api_base: this.settings.api_base || '', api_key: '' }
      } catch (error) { this.error = error.message || '读取配置失败' }
      finally { this.loading = false }
    },
    async save() {
      this.saving = true; this.error = ''; this.success = ''
      try {
        await this.$api.request('/api/llm-config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.form) })
        this.success = '配置已安全保存'
        await this.load()
        this.success = '配置已安全保存'
        this.$emit('saved')
      } catch (error) { this.error = error.message || '保存配置失败' }
      finally { this.saving = false }
    },
    async testConnection() {
      this.testing = true; this.error = ''; this.success = ''
      try {
        const result = await this.$api.request('/api/llm-config/test', { method: 'POST' })
        this.success = `${result.message} · ${result.model}`
      } catch (error) { this.error = error.message || '连接测试失败' }
      finally { this.testing = false }
    },
    async clearConfig() {
      if (!window.confirm('确定清除页面保存的 LLM 配置吗？环境变量不会受影响。')) return
      this.error = ''; this.success = ''
      try {
        await this.$api.delete('/api/llm-config')
        await this.load()
        this.success = '页面配置已清除'
        this.$emit('saved')
      } catch (error) { this.error = error.message || '清除配置失败' }
    },
  },
}
</script>

<style scoped>
.settings-backdrop { position: fixed; z-index: 80; inset: 0; display: grid; place-items: center; padding: 18px; background: #11182799; }
.settings-dialog { width: min(560px, 100%); max-height: calc(100vh - 36px); overflow: auto; background: white; border-radius: 10px; box-shadow: 0 18px 60px #0005; }
header { display: flex; justify-content: space-between; gap: 18px; padding: 20px 22px; border-bottom: 1px solid #ececf0; }
header h2 { font-size: 20px; margin-bottom: 4px; } header p { color: #777; font-size: 12px; } header button { border: 0; background: none; color: #777; font-size: 25px; cursor: pointer; }
form { display: grid; gap: 15px; padding: 20px 22px; }
label { display: grid; gap: 5px; color: #353b47; font-size: 13px; font-weight: 600; } label span { color: #9298a3; font-size: 11px; font-weight: 400; }
input { border: 1px solid #cdd2db; border-radius: 6px; padding: 9px 10px; font: inherit; }
.environment-notice { padding: 10px 12px; border: 1px solid #f1d58d; border-radius: 6px; background: #fff9e8; color: #715917; font-size: 12px; line-height: 1.5; }
.security-note { color: #777; font-size: 11px; margin: -5px 0 0; }
.status-row { display: flex; align-items: center; gap: 7px; color: #666; font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #aaa; }.status-dot.ready { background: #20a464; }
.actions { display: grid; grid-template-columns: auto 1fr auto auto; align-items: center; gap: 8px; padding-top: 5px; }
.actions button { border: 0; border-radius: 6px; padding: 8px 12px; cursor: pointer; }.actions button:disabled { opacity: .55; cursor: wait; }
.primary { background: #e94560; color: white; }.secondary { background: #eceef3; color: #333; }.danger { background: transparent; color: #a82038; }
.success { margin: 0; color: #16834b; font-size: 12px; }.loading { padding: 40px 22px; color: #777; }
@media (max-width: 560px) { .settings-backdrop { padding: 0; }.settings-dialog { width: 100vw; height: 100vh; max-height: none; border-radius: 0; }.actions { grid-template-columns: 1fr 1fr; }.actions span { display: none; }.actions button { width: 100%; } }
</style>
