import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import Knowledge from '../src/pages/Knowledge.vue'

describe('Knowledge route integration', () => {
  it('reads snapshot_id from the hash route query', async () => {
    const report = {
      api_contract: { endpoint_count: 0, endpoints: [] },
      module_topology: { module_count: 0 },
      core_entities: [],
      test_coverage: { coverage_pct: 0 },
      layer_violations: { violation_count: 0 },
    }
    const api = {
      get: vi.fn(async (path) => path === '/api/knowledge' ? report : { rules: [] }),
    }

    const wrapper = mount(Knowledge, {
      props: { repoName: 'snapshot' },
      global: {
        mocks: {
          $api: api,
          $route: { query: { snapshot_id: 'snapshot-1' } },
          $runAsync: async (task) => task(),
        },
      },
    })
    await flushPromises()

    expect(wrapper.vm.snapshotId).toBe('snapshot-1')
    expect(api.get).toHaveBeenCalledWith('/api/knowledge', {
      snapshot_id: 'snapshot-1',
      include_llm: false,
    })
    expect(wrapper.text()).not.toContain('缺少 snapshot_id')
    wrapper.unmount()
  })
})
