import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RuleMap from '../src/components/RuleMap.vue'

const MAP = {
  version: 1,
  repository_snapshot_id: 'snap-1',
  summary: {
    interface_count: 3,
    interfaces_with_rules: 2,
    rule_count: 2,
    group_count: 2,
    shared_rule_count: 1,
    coverage: 0.6667,
    integration_count: 1,
    cross_service_integration_count: 1,
  },
  service: {
    rule_count: 2,
    shared_rule_count: 1,
    rules: [
      {
        atom_id: 'a1', text: '订单金额必须大于 0', rule_type: 'validation',
        role: 'shared', groups: ['order'], source_interfaces: ['GET|/order/list|list', 'GET|/order/detail|detail'],
        evidence: [{ method: 'GET', path: '/order/list', handler: 'list', file: 'Order.java', line: 10, lines: [12] }],
      },
      {
        atom_id: 'a2', text: '只有管理员可以取消订单', rule_type: 'authorization',
        role: 'local', groups: ['order'], source_interfaces: ['POST|/order/cancel|cancel'],
        evidence: [{ method: 'POST', path: '/order/cancel', handler: 'cancel', file: 'Order.java', line: 30, lines: [] }],
      },
    ],
  },
  groups: [
    {
      group_key: 'order', interface_count: 2, interfaces_with_rules: 2, rule_count: 2,
      shared_rule_count: 1, coverage: 1,
      interfaces: [
        { interface_key: 'GET|/order/list|list', method: 'GET', path: '/order/list', rule_count: 1 },
        { interface_key: 'POST|/order/cancel|cancel', method: 'POST', path: '/order/cancel', rule_count: 1 },
      ],
      rules: [
        { atom_id: 'a1', text: '订单金额必须大于 0', rule_type: 'validation', role: 'shared',
          groups: ['order'], source_interfaces: ['GET|/order/list|list', 'GET|/order/detail|detail'], evidence: [] },
        { atom_id: 'a2', text: '只有管理员可以取消订单', rule_type: 'authorization', role: 'local',
          groups: ['order'], source_interfaces: ['POST|/order/cancel|cancel'], evidence: [] },
      ],
    },
    {
      group_key: 'user', interface_count: 1, interfaces_with_rules: 0, rule_count: 0,
      shared_rule_count: 0, coverage: 0, interfaces: [], rules: [],
    },
  ],
  interfaces: [],
  integrations: {
    total: 1,
    cross_service_count: 1,
    by_kind: [{ kind: 'http', count: 1 }],
    by_target: [{ target: 'member-service', count: 1 }],
    items: [{
      integration_id: 'i1', kind: 'http', scope: 'cross_service', target: 'member-service',
      operation: 'GET /member/{id}', input_or_key: 'userId', result_usage: '校验会员',
      source_interface: 'GET|/order/list|list', method: 'GET', path: '/order/list',
      handler: 'list', file: 'Order.java', line: 10, evidence_lines: [12],
    }],
  },
}

function mountMap() {
  const api = { get: vi.fn(async () => MAP) }
  const wrapper = mount(RuleMap, {
    props: { snapshotId: 'snap-1' },
    global: { mocks: { $api: api } },
  })
  return { wrapper, api }
}

afterEach(() => { vi.restoreAllMocks() })

describe('business rule map', () => {
  it('renders service metrics and deduplicated rules', async () => {
    const { wrapper, api } = mountMap()
    await flushPromises()

    expect(api.get).toHaveBeenCalledWith('/api/rule-map', { snapshot_id: 'snap-1' })
    expect(wrapper.text()).toContain('67%')
    expect(wrapper.text()).toContain('订单金额必须大于 0')
    expect(wrapper.text()).toContain('共享')
  })

  it('filters rules by resource group and interface', async () => {
    const { wrapper } = mountMap()
    await flushPromises()

    const orderGroup = wrapper.findAll('button.rule-group').find(button => button.text().includes('order'))
    await orderGroup.trigger('click')
    expect(wrapper.text()).toContain('2 个接口')

    const cancelChip = wrapper.findAll('button.rule-interface').find(button => button.text().includes('/order/cancel'))
    await cancelChip.trigger('click')
    expect(wrapper.text()).toContain('只有管理员可以取消订单')
    expect(wrapper.text()).not.toContain('订单金额必须大于 0')
  })

  it('shows cross-service integration contracts', async () => {
    const { wrapper } = mountMap()
    await flushPromises()

    const tab = wrapper.findAll('button').find(button => button.text().includes('跨服务集成'))
    await tab.trigger('click')

    expect(wrapper.text()).toContain('member-service')
    expect(wrapper.text()).toContain('GET /member/{id}')
    expect(wrapper.text()).toContain('跨服务')
  })
})
