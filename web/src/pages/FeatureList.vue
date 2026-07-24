<template>
  <div class="feature-list">
    <div class="header">
      <h1>功能列表</h1>
      <div class="controls">
        <input v-model="search" placeholder="搜索功能名称..." @input="searchFeatures" class="search-input" />
        <select v-model="statusFilter" @change="loadFeatures" class="filter-select">
          <option value="all">全部状态</option>
          <option value="active">活跃</option>
          <option value="removed">已移除</option>
        </select>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>功能名称</th>
            <th>类型</th>
            <th>状态</th>
            <th>事件数</th>
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
            <td><span class="status-tag" :class="f.status">{{ f.status }}</span></td>
            <td>{{ f.event_count ?? '-' }}</td>
            <td class="id-cell">{{ f.stable_id }}</td>
            <td>
              <router-link :to="'/features/' + encodeURIComponent(f.stable_id)" class="detail-link">详情</router-link>
            </td>
          </tr>
          <tr v-if="features.length === 0">
            <td colspan="6" class="empty-row">暂无功能数据</td>
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
      offset: 0,
      pageSize: 50,
    }
  },
  computed: {
    pageNum() { return Math.floor(this.offset / this.pageSize) + 1 },
    totalPages() { return Math.ceil(this.total / this.pageSize) },
  },
  created() { this.loadFeatures() },
  methods: {
    async loadFeatures() {
      const params = new URLSearchParams({
        status: this.statusFilter, search: this.search,
        limit: this.pageSize, offset: this.offset,
      });
      try {
        const r = await fetch('/api/features?' + params);
        const data = await r.json();
        this.features = data.features || [];
        this.total = data.total || 0;
      } catch(e) { console.error(e) }
    },
    searchFeatures() {
      this.offset = 0;
      this.loadFeatures();
    },
    nextPage() { this.offset += this.pageSize; this.loadFeatures() },
    prevPage() { this.offset = Math.max(0, this.offset - this.pageSize); this.loadFeatures() },
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
