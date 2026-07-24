<template>
  <div class="feature-list">
    <div class="header">
      <h1>功能列表</h1>
      <div class="controls">
        <input v-model="search" placeholder="搜索功能名称..." @input="searchFeatures" class="search-input" />
        <select v-model="selectedCommit" @change="onCommitChange" class="filter-select commit-select">
          <option value="">当前最新</option>
          <option v-for="c in commits" :key="c.hash" :value="c.hash">
            {{ c.hash.slice(0, 7) }} — {{ formatDate(c.timestamp) }} {{ c.message.slice(0, 40) }}
          </option>
        </select>
        <select v-model="statusFilter" @change="loadFeatures" class="filter-select">
          <option value="all">全部状态</option>
          <option value="active">活跃</option>
          <option value="removed">已移除</option>
        </select>
      </div>
    </div>

    <div v-if="selectedCommit" class="snapshot-banner">
      展示 commit <code>{{ selectedCommit.slice(0, 7) }}</code> 时的功能快照
      <button @click="selectedCommit=''; loadFeatures();" class="clear-btn">清除</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>功能名称</th>
            <th>类型</th>
            <th v-if="!selectedCommit">状态</th>
            <th v-if="selectedCommit">调用树节点</th>
            <th v-if="!selectedCommit">事件数</th>
            <th>稳定 ID</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="f in features" :key="f.stable_id">
            <td class="name-cell">
              <router-link :to="'/features/' + encodeURIComponent(f.stable_id)">{{ f.canonical_name }}</router-link>
            </td>
            <td><span class="type-tag">{{ f.entry_type }}</span></td>
            <td v-if="!selectedCommit"><span class="status-tag" :class="f.status">{{ f.status }}</span></td>
            <td v-if="selectedCommit">{{ f.call_tree_nodes ?? '-' }}</td>
            <td v-if="!selectedCommit">{{ f.event_count ?? '-' }}</td>
            <td class="id-cell">{{ f.stable_id }}</td>
            <td>
              <router-link :to="'/features/' + encodeURIComponent(f.stable_id)" class="detail-link">详情</router-link>
            </td>
          </tr>
          <tr v-if="features.length === 0">
            <td :colspan="selectedCommit ? 5 : 6" class="empty-row">
              {{ selectedCommit ? '该 commit 下暂无功能数据' : '暂无功能数据' }}
            </td>
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
  data() {
    return {
      features: [],
      total: 0,
      search: '',
      statusFilter: 'all',
      selectedCommit: '',
      commits: [],
      offset: 0,
      pageSize: 50,
    }
  },
  computed: {
    pageNum() { return Math.floor(this.offset / this.pageSize) + 1 },
    totalPages() { return Math.ceil(this.total / this.pageSize) },
  },
  async created() {
    await this.loadCommits();
    this.loadFeatures();
  },
  methods: {
    async loadCommits() {
      try {
        const r = await fetch('/api/commits?limit=200');
        const data = await r.json();
        this.commits = data.commits || [];
      } catch(e) { console.error(e) }
    },
    async loadFeatures() {
      const params = new URLSearchParams({
        status: this.statusFilter, search: this.search,
        limit: this.pageSize, offset: this.offset,
      });
      if (this.selectedCommit) {
        params.set('at_commit', this.selectedCommit);
      }
      try {
        const r = await fetch('/api/features?' + params);
        const data = await r.json();
        this.features = data.features || [];
        this.total = data.total || 0;
      } catch(e) { console.error(e) }
    },
    searchFeatures() { this.offset = 0; this.loadFeatures(); },
    onCommitChange() { this.offset = 0; this.loadFeatures(); },
    nextPage() { this.offset += this.pageSize; this.loadFeatures() },
    prevPage() { this.offset = Math.max(0, this.offset - this.pageSize); this.loadFeatures() },
    formatDate(ts) { return new Date(ts * 1000).toLocaleDateString('zh-CN'); },
  },
}
</script>

<style scoped>
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
.header h1 { font-size: 24px; }
.controls { display: flex; gap: 10px; flex-wrap: wrap; }
.search-input { padding: 6px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; width: 200px; }
.filter-select { padding: 6px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; background: #fff; }
.commit-select { max-width: 360px; font-size: 12px; font-family: monospace; }
.snapshot-banner { background: #fff3cd; border: 1px solid #ffc107; border-radius: 6px; padding: 8px 16px; margin-bottom: 16px; font-size: 13px; color: #856404; display: flex; align-items: center; gap: 12px; }
.snapshot-banner code { background: #ffeeba; padding: 1px 6px; border-radius: 3px; }
.clear-btn { padding: 2px 10px; border: 1px solid #ffc107; border-radius: 4px; background: #fff; cursor: pointer; font-size: 12px; color: #856404; }
.clear-btn:hover { background: #ffc107; color: #fff; }
.table-wrap { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 12px 16px; font-size: 12px; font-weight: 600; color: #888; text-transform: uppercase; background: #fafafa; border-bottom: 1px solid #eee; }
td { padding: 12px 16px; font-size: 14px; border-bottom: 1px solid #f0f0f0; }
.name-cell a { color: #e94560; text-decoration: none; font-weight: 500; }
.name-cell a:hover { text-decoration: underline; }
.type-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; background: #e8e8e8; color: #555; }
.status-tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.status-tag.active { background: #d4edda; color: #155724; }
.status-tag.removed { background: #f8d7da; color: #721c24; }
.id-cell { font-family: monospace; font-size: 12px; color: #888; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detail-link { color: #e94560; text-decoration: none; font-size: 13px; }
.empty-row { text-align: center; color: #999; padding: 40px 0 !important; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 16px; margin-top: 20px; font-size: 14px; color: #666; }
.pagination button { padding: 6px 16px; border: 1px solid #ddd; border-radius: 6px; background: #fff; cursor: pointer; font-size: 13px; }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination button:not(:disabled):hover { border-color: #e94560; color: #e94560; }
</style>
