import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import Knowledge from '../src/pages/Knowledge.vue'

const callChainApi = vi.hoisted(() => ({
  get: vi.fn(),
  request: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('../src/api/apiClient.js', () => ({ apiClient: callChainApi }))

describe('API explanation call-chain rail', () => {
  it('uses the endpoint node_id and renders local plus aggregate explanations in the right rail', async () => {
    callChainApi.get.mockImplementation(async (path) => {
      if (path === '/api/call-tree/rule') {
        return {
          source: {
            file: 'orders.py', start_line: 10, end_line: 12,
            content: 'def get_orders():\n    validate_order()\n    return save_order()',
          },
        }
      }
      return {}
    })
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
    await flushPromises()
    expect(wrapper.text()).toContain('节点自身翻译')
    expect(wrapper.text()).toContain('校验订单请求')
    expect(wrapper.text()).toContain('校验订单请求并创建订单')
    expect(wrapper.find('.nrr-api-mode .primary').text()).toContain('手动刷新 API 解释')
    expect(wrapper.text()).toContain('节点源码')
    expect(wrapper.text()).toContain('orders.py:10-12')
    expect(wrapper.find('.nsp-code').text()).toContain('def get_orders()')
    expect(callChainApi.get).toHaveBeenCalledWith('/api/call-tree/rule', {
      snapshot_id: 'repo-snapshot', node_id: 'fn-root', view_id: '',
    })

    const divider = tree.find('[data-testid="call-chain-divider-0"]')
    expect(tree.findAll('.ct-divider')).toHaveLength(2)
    const split = tree.find('.ct-split')
    const beforeResize = split.attributes('style')
    await divider.trigger('keydown.right')
    expect(split.attributes('style')).not.toBe(beforeResize)

    await wrapper.find('.nrr-api-mode .primary').trigger('click')
    expect(api.request).toHaveBeenCalledWith('/api/api-explanations/generate', expect.objectContaining({ method: 'POST' }))
    wrapper.unmount()
  })
})
