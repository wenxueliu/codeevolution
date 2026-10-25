<template>
  <div class="layers-page">
    <UiState
      v-if="error"
      kind="error"
      :title="t('分层规则加载失败')"
      :message="error"
      :action-label="t('重试')"
      :dismiss-label="t('关闭')"
      @action="reload"
      @dismiss="error = null"
    />

    <header class="layers-header">
      <div>
        <h1>{{ t('分层规则') }}</h1>
        <p>{{ t('按仓库声明分层与依赖方向，在线查看和编辑；保存后知识提取立即使用。') }}</p>
      </div>
      <div class="layers-selectors">
        <label>
          {{ t('代码仓') }}
          <select v-model="repoName" @change="onRepoChange">
            <option v-for="repo in repos" :key="repo.name" :value="repo.name">{{ repo.name }}</option>
          </select>
        </label>
        <label v-if="members.length > 1">
          {{ t('成员') }}
          <select v-model="member" @change="load">
            <option v-for="item in members" :key="item.path" :value="item.path">{{ item.path }}</option>
          </select>
        </label>
      </div>
    </header>

    <p v-if="!repos.length && !loading" class="layers-empty">
      {{ t('还没有代码仓，请先在首页添加。') }}
      <router-link to="/">{{ t('前往首页') }}</router-link>
    </p>

    <template v-else-if="meta">
      <section class="layers-meta">
        <div><span>{{ t('配置文件') }}</span><code>{{ meta.config_path }}</code></div>
        <div>
          <span>{{ t('状态') }}</span>
          <strong :class="meta.exists ? 'ok' : 'warn'">
            {{ meta.exists ? t('仓库声明') : t('内置默认（未声明）') }}
          </strong>
        </div>
        <div><span>{{ t('基线') }}</span><strong>{{ t('{count} 对', { count: meta.baseline_pairs }) }}</strong></div>
        <div v-if="generated">
          <span>{{ t('覆盖率') }}</span>
          <strong>{{ Math.round((generated.coverage || 0) * 100) }}%</strong>
        </div>
      </section>

      <p v-if="!meta.graph_ready" class="layers-warning">
        {{ t('该代码仓尚未建立 CodeGraph 索引，无法生成或检查；请先运行 codegraph init。') }}
      </p>

      <div class="layers-toolbar">
        <button class="secondary" :disabled="busy || !meta.graph_ready" @click="regenerate">
          {{ busy === 'init' ? t('生成中...') : t('重新生成声明') }}
        </button>
        <button class="secondary" :disabled="busy" @click="format">{{ t('格式化') }}</button>
        <button class="secondary" :disabled="busy || !meta.graph_ready" @click="check">
          {{ busy === 'check' ? t('检查中...') : t('检查') }}
        </button>
        <button class="secondary" :disabled="busy || !meta.graph_ready" @click="acceptBaseline">
          {{ busy === 'baseline' ? t('写入中...') : t('把当前违规写入基线') }}
        </button>
        <button class="primary" :disabled="busy || !dirty" @click="save">
          {{ busy === 'save' ? t('保存中...') : t('保存') }}
        </button>
        <span class="layers-dirty">{{ dirty ? t('未保存的修改') : t('已保存') }}</span>
      </div>

      <p v-if="message" class="layers-message">{{ message }}</p>

      <div class="layers-grid">
        <section class="layers-panel">
          <div class="layers-panel-heading">
            <h2>{{ t('分层声明') }}</h2>
            <span>{{ t('JSON · {count} 行', { count: editorLineCount }) }}</span>
          </div>
          <textarea
            v-model="editor"
            class="layers-editor"
            spellcheck="false"
            :aria-label="t('分层声明')"
          />
        </section>

        <section class="layers-panel">
          <div class="layers-panel-heading">
            <h2>{{ t('违规') }}</h2>
            <span v-if="result">
              {{ t('新增 {count} 条 · 基线 {base} 条', { count: result.new_count, base: result.baselined }) }}
            </span>
          </div>
          <div v-if="!result" class="layers-placeholder">{{ t('点击“检查”查看当前声明的违规结果。') }}</div>
          <div v-else-if="!result.groups.length" class="layers-placeholder ok">
            {{ t('没有新增违规') }}
          </div>
          <div v-else class="layers-violations">
            <div v-for="group in result.groups" :key="group.from + '->' + group.to" class="layers-group">
              <div class="layers-group-heading">
                <strong>{{ group.from }}</strong> → <strong>{{ group.to }}</strong>
                <span>{{ t('共 {count} 条', { count: group.count }) }}</span>
              </div>
              <table>
                <tbody>
                  <tr v-for="(item, index) in group.items" :key="index">
                    <td><code>{{ item.source_file }}<template v-if="item.call_line">:{{ item.call_line }}</template></code></td>
                    <td class="arrow">→</td>
                    <td><code>{{ item.target_file }}</code></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </div>
    </template>

    <UiState v-else-if="loading" kind="loading" :title="t('正在加载分层声明')" />
  </div>
</template>

<script>
import UiState from '../components/UiState.vue'
import { t } from '../i18n.js'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export default {
  components: { UiState },
  data: () => ({
    repos: [],
    repoName: '',
    member: '',
    meta: null,
    editor: '',
    savedEditor: '',
    result: null,
    message: '',
    error: null,
    loading: false,
    busy: '',
  }),
  computed: {
    members() {
      const repo = this.repos.find((item) => item.name === this.repoName)
      return repo?.repositories?.length ? repo.repositories : []
    },
    dirty() {
      return this.editor !== this.savedEditor
    },
    editorLineCount() {
      return this.editor ? this.editor.split('\n').length : 0
    },
    generated() {
      try {
        return JSON.parse(this.editor).generated || null
      } catch {
        return null
      }
    },
  },
  created() {
    this.reload()
  },
  methods: {
    t,
    async reload() {
      this.loading = true
      this.error = null
      try {
        const data = await this.$api.get('/api/repos')
        this.repos = data.repos || []
        if (!this.repos.find((item) => item.name === this.repoName)) {
          this.repoName = this.repos[0]?.name || ''
        }
        if (this.repoName) await this.load()
      } catch (error) {
        this.error = error.message
      } finally {
        this.loading = false
      }
    },
    onRepoChange() {
      this.member = ''
      this.load()
    },
    async load() {
      if (!this.repoName) return
      this.loading = true
      this.error = null
      try {
        const data = await this.$api.get(`/api/repos/${encodeURIComponent(this.repoName)}/layers`, {
          member: this.member,
        })
        this.meta = data
        this.editor = JSON.stringify(data.config, null, 2)
        this.savedEditor = this.editor
        this.result = null
        this.message = ''
      } catch (error) {
        this.error = error.message
        this.meta = null
      } finally {
        this.loading = false
      }
    },
    async regenerate() {
      this.busy = 'init'
      this.error = null
      this.message = ''
      try {
        const data = await this.$api.request(
          `/api/repos/${encodeURIComponent(this.repoName)}/layers/init`,
          { method: 'POST', query: { member: this.member } },
        )
        this.editor = JSON.stringify(data.generated, null, 2)
        this.message = t('生成结果已放入编辑器，确认后点击保存。')
      } catch (error) {
        this.error = error.message
      } finally {
        this.busy = ''
      }
    },
    format() {
      const config = this.parseEditor()
      if (config) this.editor = JSON.stringify(config, null, 2)
    },
    parseEditor() {
      try {
        const value = JSON.parse(this.editor)
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
          this.message = t('声明必须是一个 JSON 对象。')
          return null
        }
        return value
      } catch (error) {
        this.message = t('JSON 格式错误：{message}', { message: error.message })
        return null
      }
    },
    async save() {
      const config = this.parseEditor()
      if (!config) return
      this.busy = 'save'
      this.error = null
      this.message = ''
      try {
        await this.$api.request(`/api/repos/${encodeURIComponent(this.repoName)}/layers`, {
          method: 'PUT',
          query: { member: this.member },
          headers: JSON_HEADERS,
          body: JSON.stringify({ config }),
        })
        this.savedEditor = this.editor
        this.meta = { ...this.meta, exists: true }
        this.message = t('声明已保存')
      } catch (error) {
        this.message = t('保存失败：{message}', { message: error.message })
      } finally {
        this.busy = ''
      }
    },
    async check() {
      const config = this.parseEditor()
      if (!config) return
      this.busy = 'check'
      this.error = null
      this.message = ''
      try {
        this.result = await this.$api.request(
          `/api/repos/${encodeURIComponent(this.repoName)}/layers/check`,
          {
            method: 'POST',
            query: { member: this.member },
            headers: JSON_HEADERS,
            body: JSON.stringify({ config }),
          },
        )
        this.message = this.result.new_count
          ? t('检查完成：{count} 条新增违规', { count: this.result.new_count })
          : t('没有新增违规')
      } catch (error) {
        this.error = error.message
      } finally {
        this.busy = ''
      }
    },
    async acceptBaseline() {
      const config = this.parseEditor()
      if (!config) return
      this.busy = 'baseline'
      this.error = null
      this.message = ''
      try {
        const data = await this.$api.request(
          `/api/repos/${encodeURIComponent(this.repoName)}/layers/baseline`,
          {
            method: 'POST',
            query: { member: this.member },
            headers: JSON_HEADERS,
            body: JSON.stringify({ config }),
          },
        )
        this.meta = { ...this.meta, baseline_pairs: data.pairs }
        this.message = t('基线已更新：{count} 对', { count: data.pairs })
        this.result = null
      } catch (error) {
        this.error = error.message
      } finally {
        this.busy = ''
      }
    },
  },
}
</script>

<style>
.layers-page { padding: 0; }
.layers-header { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; margin-bottom: 18px; }
.layers-header h1 { margin: 0 0 6px; font-size: 24px; }
.layers-header p { margin: 0; color: #777; font-size: 13px; max-width: 640px; }
.layers-selectors { display: flex; gap: 12px; }
.layers-selectors label { display: grid; gap: 5px; color: #555; font-size: 12px; }
.layers-selectors select { border: 1px solid #cfd3da; border-radius: 5px; padding: 7px 8px; min-width: 150px; }
.layers-meta { display: flex; gap: 1px; background: #e7e7eb; border: 1px solid #e7e7eb; border-radius: 8px; overflow: hidden; margin-bottom: 14px; }
.layers-meta > div { flex: 1; background: #fff; padding: 12px 14px; min-width: 0; }
.layers-meta span { display: block; color: #888; font-size: 11px; margin-bottom: 5px; }
.layers-meta code { display: block; color: #444; font-size: 12px; overflow-wrap: anywhere; }
.layers-meta strong { font-size: 14px; }
.layers-meta .ok { color: #23764a; }
.layers-meta .warn { color: #8a6418; }
.layers-warning { margin: 0 0 14px; padding: 10px 14px; background: #fff3db; border-radius: 6px; color: #8a6418; font-size: 13px; }
.layers-toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.layers-toolbar .primary, .layers-toolbar .secondary { border-radius: 6px; padding: 8px 13px; cursor: pointer; }
.layers-toolbar .primary { border: 0; background: #e94560; color: #fff; }
.layers-toolbar .secondary { border: 1px solid #cdd2dc; background: #fff; color: #444; }
.layers-toolbar button:disabled { opacity: .5; cursor: not-allowed; }
.layers-dirty { color: #888; font-size: 12px; margin-left: auto; }
.layers-message { margin: 0 0 12px; padding: 9px 12px; background: #eef6f1; border-radius: 6px; color: #23764a; font-size: 13px; }
.layers-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.layers-panel { background: #fff; border: 1px solid #e7e7eb; border-radius: 8px; overflow: hidden; }
.layers-panel-heading { display: flex; justify-content: space-between; align-items: baseline; padding: 14px 16px; border-bottom: 1px solid #eee; }
.layers-panel-heading h2 { margin: 0; font-size: 15px; }
.layers-panel-heading span { color: #888; font-size: 12px; }
.layers-editor { display: block; width: 100%; min-height: 460px; border: 0; padding: 14px 16px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.6; resize: vertical; outline: none; }
.layers-placeholder { padding: 30px 16px; text-align: center; color: #888; font-size: 13px; }
.layers-placeholder.ok { color: #23764a; }
.layers-violations { max-height: 520px; overflow: auto; padding: 8px 0; }
.layers-group + .layers-group { border-top: 1px solid #eee; margin-top: 6px; padding-top: 6px; }
.layers-group-heading { display: flex; gap: 8px; align-items: baseline; padding: 8px 16px; font-size: 13px; }
.layers-group-heading span { margin-left: auto; color: #888; font-size: 12px; }
.layers-violations table { width: 100%; border-collapse: collapse; font-size: 12px; }
.layers-violations td { padding: 4px 8px; vertical-align: top; }
.layers-violations td:first-child { padding-left: 16px; }
.layers-violations td.arrow { color: #bbb; width: 16px; padding: 4px 0; }
.layers-violations code { color: #444; overflow-wrap: anywhere; }
.layers-empty { color: #888; padding: 30px 0; }
@media (max-width: 1000px) {
  .layers-grid { grid-template-columns: 1fr; }
  .layers-header { flex-direction: column; }
  .layers-meta { flex-wrap: wrap; }
}
</style>
