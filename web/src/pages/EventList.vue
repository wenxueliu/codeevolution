<template>
  <div class="event-list-page">
    <div v-if="error" class="request-error">{{ error.message }}</div>
    <div v-if="loading" class="request-loading">加载中...</div>
    <div class="header">
      <h1>事件日志</h1>
      <div class="controls">
        <input v-model="searchFeature" placeholder="功能名称..." class="search-input" @input="loadEvents" />
        <select v-model="eventTypeFilter" @change="loadEvents" class="filter-select">
          <option value="">全部事件类型</option>
          <option value="BORN">BORN</option>
          <option value="DIED">DIED</option>
          <option value="GROWN">GROWN</option>
          <option value="SHRUNK">SHRUNK</option>
          <option value="EXTENDED">EXTENDED</option>
          <option value="CONTRACTED">CONTRACTED</option>
          <option value="DEP_CREATED">DEP_CREATED</option>
          <option value="DEP_REMOVED">DEP_REMOVED</option>
          <option value="MOVED">MOVED</option>
          <option value="MODIFIED">MODIFIED</option>
        </select>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>事件</th>
            <th>功能</th>
            <th>Commit</th>
            <th>作者</th>
            <th>时间</th>
            <th>消息</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="ev in events" :key="ev.id">
            <td><span class="event-badge" :class="ev.event_type">{{ ev.event_type }}</span></td>
            <td>
              <router-link :to="'/repo/' + repoName + '/features/' + encodeURIComponent(ev.stable_id)" class="feature-link">
                {{ ev.canonical_name }}
              </router-link>
            </td>
            <td class="commit-cell">{{ ev.commit_hash?.slice(0, 7) }}</td>
            <td>{{ ev.author }}</td>
            <td class="date-cell">{{ formatDate(ev.timestamp) }}</td>
            <td class="msg-cell">{{ ev.message?.slice(0, 60) }}</td>
          </tr>
          <tr v-if="events.length === 0">
            <td colspan="6" class="empty-row">暂无事件数据</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pagination" v-if="total > pageSize">
      <button @click="prevPage" :disabled="offset === 0">上一页</button>
      <span>第 {{ pageNum }} / {{ totalPages }} 页 (共 {{ total }} 项)</span>
      <button @click="nextPage" :disabled="offset + pageSize >= total">下一页</button>
    </div>
  </div>
</template>

<script>
export default {
  props: { repoName: String },
  data() {
    return {
      events: [], total: 0, offset: 0, pageSize: 50,
      searchFeature: '', eventTypeFilter: '',
    }
  },
  computed: {
    pageNum() { return Math.floor(this.offset / this.pageSize) + 1 },
    totalPages() { return Math.ceil(this.total / this.pageSize) },
  },
  created() { this.loadEvents() },
  methods: {
    async loadEvents() {
      await this.$runAsync(async () => {
        const data = await this.$api.get('/api/events', {
          repo: this.repoName || '', feature_stable_id: this.searchFeature,
          event_type: this.eventTypeFilter, limit: this.pageSize, offset: this.offset,
        })
        this.events = data.events || [];
        this.total = data.total || 0;
      })
    },
    nextPage() { this.offset += this.pageSize; this.loadEvents() },
    prevPage() { this.offset = Math.max(0, this.offset - this.pageSize); this.loadEvents() },
    formatDate(ts) { return new Date(ts * 1000).toLocaleDateString('zh-CN') },
  },
}
</script>

<style scoped>
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.header h1 { font-size: 24px; }
.controls { display: flex; gap: 10px; }
.search-input { padding: 6px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; width: 240px; }
.filter-select { padding: 6px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; background: #fff; }
.table-wrap { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 12px 16px; font-size: 12px; font-weight: 600; color: #888; text-transform: uppercase; background: #fafafa; border-bottom: 1px solid #eee; }
td { padding: 10px 16px; font-size: 13px; border-bottom: 1px solid #f0f0f0; }
.event-badge { padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; background: #eee; color: #555; white-space: nowrap; }
.event-badge.BORN { background: #d4edda; color: #155724; }
.event-badge.DIED { background: #f8d7da; color: #721c24; }
.event-badge.GROWN { background: #fff3cd; color: #856404; }
.feature-link { color: #e94560; text-decoration: none; }
.feature-link:hover { text-decoration: underline; }
.commit-cell { font-family: monospace; font-size: 12px; color: #888; }
.date-cell { color: #888; font-size: 12px; white-space: nowrap; }
.msg-cell { color: #999; font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-row { text-align: center; color: #999; padding: 40px 0 !important; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 16px; margin-top: 20px; font-size: 14px; color: #666; }
.pagination button { padding: 6px 16px; border: 1px solid #ddd; border-radius: 6px; background: #fff; cursor: pointer; font-size: 13px; }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination button:not(:disabled):hover { border-color: #e94560; color: #e94560; }
</style>
