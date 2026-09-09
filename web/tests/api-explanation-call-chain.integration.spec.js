import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import Knowledge from '../src/pages/Knowledge.vue'

describe('API explanation call-chain rail', () => {
  it('uses the endpoint node_id and renders local plus aggregate explanations in the right rail', async () => {
    const api = {
      get: vi.fn(async (path) => {
        if (path === '/api/knowledge') {
          return {
            api_contract: {
              endpoint_count: 1,
              endpoints: [{
                method: 'GET', path: '/orders', handler: 'orders::get_orders',
                node_id: 'fn-root', file: 'orders.py', line: 10,
              }],
            },
            module_topology: {}, core_entities: [], test_coverage: {}, layer_violations: {},
          }
        }
        if (path === '/api/api-explanations/current') {
          return {
            snapshot: {
              id: 'explanation-1', status: 'completed',
              nodes: [{
                node_key: 'repo-snapshot::fn-root', status: 'completed',
                local_explanation: { summary: '校验订单请求' },
                aggregate_explanation: { summary: '校验订单请求并创建订单', main_flow: ['校验请求', '创建订单'] },
              }],
            },
          }
        }
        if (path === '/api/api-explanations/snapshots') return { snapshots: [] }
        if (path === '/api/business-rules') return { rules: [] }
        return {}
      }),
      request: vi.fn(async () => ({ id: 'explanation-2', status: 'running' })),
      delete: vi.fn(async () => ({ ok: true })),
    }
    const wrapper = mount(Knowledge, {
      props: { repoName: 'orders' },
      global: {
        mocks: {
          $api: api,
          $route: { query: { snapshot_id: 'repo-snapshot' } },
          $runAsync: async (task) => task(),
        },
        stubs: {
          'el-tree': { template: '<div class="tree-stub"></div>' },
        },
      },
    })
    await flushPromises()

    await wrapper.find('tr.clickable').trigger('click')
    await flushPromises()
    // The real tree request is built from the endpoint's graph id, not its label.
    const tree = wrapper.findComponent({ name: 'CallChainTree' })
    expect(tree.props('label').node_id).toBe('fn-root')

    await wrapper.find('.ct-root').trigger('click')
    expect(wrapper.text()).toContain('节点自身翻译')
    expect(wrapper.text()).toContain('校验订单请求')
    expect(wrapper.text()).toContain('校验订单请求并创建订单')
    expect(wrapper.find('.nrr-api-mode .primary').text()).toContain('手动刷新 API 解释')

    await wrapper.find('.nrr-api-mode .primary').trigger('click')
    expect(api.request).toHaveBeenCalledWith('/api/api-explanations/generate', expect.objectContaining({ method: 'POST' }))
    wrapper.unmount()
  })
})
