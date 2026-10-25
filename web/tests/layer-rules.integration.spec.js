import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import LayerRules from '../src/components/LayerRules.vue'

function mountRules({ get = {}, request = {} } = {}) {
  const api = {
    get: vi.fn(async (path) => get[path] ?? {}),
    request: vi.fn(async (path) => request[path] ?? {}),
  }
  const wrapper = mount(LayerRules, {
    props: { snapshotId: 'snap-1' },
    global: { mocks: { $api: api } },
  })
  return { wrapper, api }
}

const META = {
  snapshot_id: 'snap-1',
  config_path: '/repo/.codeevolution/layers.json',
  exists: true,
  baseline_pairs: 2,
  config: {
    version: 'layers/v1',
    layers: { presentation: ['**/web/**'], domain: ['**/domain/**'] },
    rules: { strict: true, allow: [], ignore: [] },
  },
}

const RULES_URL = '/api/repository-snapshots/snap-1/layers'

afterEach(() => { vi.restoreAllMocks() })

describe('layer rules inside the layer violations section', () => {
  it('loads the declaration scoped to the current snapshot', async () => {
    const { wrapper, api } = mountRules({ get: { [RULES_URL]: META } })
    await flushPromises()

    expect(api.get).toHaveBeenCalledWith(RULES_URL)
    expect(wrapper.text()).toContain('分层规则')
    expect(wrapper.text()).toContain('仓库声明')
    expect(wrapper.find('textarea').element.value).toContain('**/web/**')
  })

  it('saves the edited declaration to the current repository config', async () => {
    const { wrapper, api } = mountRules({
      get: { [RULES_URL]: META },
      request: { [RULES_URL]: { ok: true } },
    })
    await flushPromises()
    const saveButton = () => wrapper.findAll('button').find((button) => button.text() === '保存')
    expect(saveButton().attributes('disabled')).toBeDefined()

    await wrapper.find('textarea').setValue(
      JSON.stringify({ ...META.config, rules: { strict: false } }, null, 2),
    )
    expect(saveButton().attributes('disabled')).toBeUndefined()
    await saveButton().trigger('click')
    await flushPromises()

    const call = api.request.mock.calls.find(([path]) => path === RULES_URL)
    expect(call[1].method).toBe('PUT')
    expect(wrapper.text()).toContain('声明已保存')
  })

  it('checks the draft against the snapshot graph and lists violations', async () => {
    const { wrapper } = mountRules({
      get: { [RULES_URL]: META },
      request: {
        [`${RULES_URL}/check`]: {
          origin: 'draft',
          new_count: 1,
          baselined: 0,
          groups: [{
            from: 'presentation',
            to: 'domain',
            count: 1,
            items: [{ source_file: 'app/web/orders.py', target_file: 'app/domain/order.py', call_line: 12 }],
          }],
        },
      },
    })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '检查').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('1 条新增违规')
    expect(wrapper.text()).toContain('app/web/orders.py:12')
  })
})
