import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'

import App from '../src/App.vue'
import GraphView from '../src/pages/GraphView.vue'
import Home from '../src/pages/Home.vue'
import Knowledge from '../src/pages/Knowledge.vue'
import LLMSettings from '../src/components/LLMSettings.vue'
import RepositoryAssistant from '../src/components/RepositoryAssistant.vue'
import Snapshots from '../src/pages/Snapshots.vue'

const RouterLink = {
  props: ['to'],
  computed: {
    href() {
      if (typeof this.to === 'string') return this.to
      if (this.to?.name === 'snapshots') return '/snapshots'
      if (this.to?.name === 'knowledge-home') return '/knowledge'
      if (this.to?.name === 'graph-view') return this.to.params?.viewId ? `/graph-views/${this.to.params.viewId}` : '/graph-views'
      if (this.to?.name === 'terms') return '/terms'
      if (this.to?.name === 'knowledge') return `/repo/${this.to.params.repoName}?snapshot_id=${this.to.query?.snapshot_id}`
      return '/'
    },
  },
  template: '<a :href="href"><slot /></a>',
}

function mountUi(component, responses = {}, props = {}, route = { query: { snapshot_id: 'snapshot-1' } }, options = {}) {
  const resolve = async (path, options) => {
    const value = responses[path]
    if (typeof value === 'function') return value(options, path)
    return value ?? {}
  }
  const api = {
    get: vi.fn(resolve),
    request: vi.fn(resolve),
    delete: vi.fn(async () => ({ ok: true })),
  }
  const wrapper = mount(component, {
    props,
    global: {
      components: { RouterLink },
      mixins: [{ data: () => ({ loading: false, error: null }) }],
      mocks: {
        $api: api,
        $route: route,
        $router: { push: vi.fn() },
        $runAsync: options.runAsync || async function (task) {
          this.error = null
          try { return await task() } catch (error) { this.error = error; return undefined }
        },
      },
    },
  })
  return { wrapper, api }
}

const scopeResponse = (scopeId = 'scope-shop', name = 'shop') => ({ scopes: [{ id: scopeId, name }] })
const memberResponse = (id = 'member-shop', name = 'shop') => ({ members: [{ id, display_name: name, registered_path: `/workspace/${name}` }] })

afterEach(() => {
  window.sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('PRD UI acceptance journey', () => {
  it('UI-001 enters repository registration from the empty state', async () => {
    const { wrapper } = mountUi(Home, { '/api/scopes': { scopes: [] } })
    await flushPromises()
    await wrapper.find('.empty-state .action-btn').trigger('click')
    expect(wrapper.find('[data-testid="register-form"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="repository-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="repository-path"]').exists()).toBe(true)
  })

  it('UI-002 registers a repository without modifying its path', async () => {
    const { wrapper, api } = mountUi(Home, {
      '/api/scopes': scopeResponse(),
      '/api/scopes/scope-shop/members': memberResponse(),
    })
    await flushPromises()
    await wrapper.find('[data-testid="add-repository"]').trigger('click')
    await wrapper.find('[data-testid="repository-name"]').setValue('shop')
    await wrapper.find('[data-testid="repository-path"]').setValue('/workspace/shop')
    await wrapper.find('[data-testid="register-form"]').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/scopes/scope-shop/members', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ display_name: 'shop', registered_path: '/workspace/shop' }),
    }))
  })

  it('UI-003 runs the first analysis and exposes CodeGraph initialization progress', async () => {
    const run = { id: 'run-1', status: 'running', members: [{ member_id: 'member-shop', attempt: { status: 'running', stage: 'codegraph_init' } }] }
    const { wrapper } = mountUi(Snapshots, {
      '/api/scopes': scopeResponse(),
      '/api/scopes/scope-shop/members': memberResponse(),
      '/api/repository-members/member-shop/snapshots': { items: [] },
      '/api/analysis-runs': { run },
    })
    await flushPromises()
    await wrapper.find('input[aria-label="选择 shop"]').setValue(true)
    await wrapper.findAll('.actions button').find((button) => button.text() === '运行分析').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="analysis-run"]').text()).toContain('初始化 CodeGraph')
  })

  it('UI-004 shows a completed analysis Run and published Snapshot state', async () => {
    const { wrapper } = mountUi(Snapshots, {
      '/api/scopes': scopeResponse(),
      '/api/scopes/scope-shop/members': memberResponse(),
      '/api/repository-members/member-shop/snapshots': { items: [{ id: 'snapshot-shop' }] },
    })
    await flushPromises()
    wrapper.vm.setActiveRun({ id: 'run-complete', status: 'completed', members: [{ member_id: 'member-shop', attempt: { status: 'completed', stage: 'finished' } }] })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('已发布')
    expect(wrapper.text()).toContain('snapshot-shop')
    expect(wrapper.text()).toContain('已完成')
  })

  it('UI-005 creates a Graph View and keeps the Snapshot knowledge link', async () => {
    const { wrapper, api } = mountUi(Snapshots, {
      '/api/scopes': scopeResponse(),
      '/api/scopes/scope-shop/members': memberResponse(),
      '/api/repository-members/member-shop/snapshots': { items: [{ id: 'snapshot-shop' }] },
      '/api/graph-views/current': { view: { id: 'view-shop', digest: 'sha256:view' } },
    })
    await flushPromises()
    wrapper.vm.selectedIds = ['member-shop']
    await wrapper.vm.createView()
    await wrapper.vm.$nextTick()
    expect(api.request).toHaveBeenCalledWith('/api/graph-views/current', expect.objectContaining({ method: 'POST' }))
    expect(wrapper.find('a[href="/graph-views/view-shop"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/repo/shop?snapshot_id=snapshot-shop"]').exists()).toBe(true)
  })

  it('UI-006 searches an API contract and expands its call-chain evidence', async () => {
    const endpoint = { method: 'POST', path: '/api/orders', handler: 'OrderController.create', call_chain: [{ id: 'node-1', name: 'create' }] }
    const { wrapper } = mountUi(Knowledge, { '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [endpoint] } } }, { repoName: 'shop' })
    await flushPromises()
    await wrapper.find('input[placeholder="路径、处理函数或仓库"]').setValue('orders')
    expect(wrapper.text()).toContain('/api/orders')
    await wrapper.find('tbody tr').trigger('click')
    expect(wrapper.text()).toContain('OrderController.create')
  })

  it('UI-007 exposes explicit API explanation generation', async () => {
    const endpoint = { method: 'POST', path: '/orders', handler: 'OrderController.create', repository: 'shop', file: 'Order.java', line: 10 }
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { wrapper, api } = mountUi(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [endpoint] } },
      '/api/api-explanations/current': { status: 'missing' },
      '/api/api-explanations/snapshots': { snapshots: [] },
      '/api/api-explanations/generate': { id: 'explanation-1', status: 'running' },
    }, { repoName: 'shop' })
    await flushPromises()
    await wrapper.find('tbody tr').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="explanation-generate"]').trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/api-explanations/generate', expect.objectContaining({ method: 'POST' }))
  })

  it('UI-007a edits an endpoint prompt from the default and sends it to generation', async () => {
    const endpoint = { method: 'POST', path: '/orders', handler: 'OrderController.create', repository: 'shop', file: 'Order.java', line: 10 }
    const { wrapper, api } = mountUi(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [endpoint] } },
      '/api/api-explanations/current': { status: 'missing' },
      '/api/api-explanations/snapshots': { snapshots: [] },
      '/api/api-explanations/generate': { id: 'explanation-custom', status: 'running' },
    }, { repoName: 'shop' })
    await flushPromises()
    await wrapper.find('tbody tr').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="endpoint-prompt-button"]').trigger('click')
    expect(wrapper.find('[data-testid="endpoint-prompt-dialog"]').exists()).toBe(true)
    const textarea = wrapper.find('[data-testid="endpoint-prompt-textarea"]')
    expect(textarea.element.value).toContain('业务目的')
    await textarea.setValue('请重点关注库存扣减和异常回滚')
    await wrapper.find('[data-testid="endpoint-prompt-apply"]').trigger('click')
    await flushPromises()
    const request = api.request.mock.calls.find(([path]) => path === '/api/api-explanations/generate')
    expect(JSON.parse(request[1].body).custom_prompt).toBe('请重点关注库存扣减和异常回滚')
    expect(wrapper.find('[data-testid="endpoint-prompt-dialog"]').exists()).toBe(false)
  })

  it('UI-008 displays default API explanation templates and supports fine-tuning from them', async () => {
    const defaults = { local: 'DEFAULT LOCAL', synthesis: 'DEFAULT SYNTHESIS', aggregate: 'DEFAULT AGGREGATE' }
    const endpoint = { method: 'POST', path: '/orders', handler: 'OrderController.create', repository: 'shop', file: 'Order.java', line: 10 }
    const { wrapper, api } = mountUi(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [endpoint] } },
      '/api/api-explanation-prompts': (options) => options?.method === 'POST'
        ? { profile: { id: 'prompt-2', version: 2, prompt_text: '', prompt_templates: defaults } }
        : { current: { id: 'prompt-1', version: 1, prompt_text: '', prompt_templates: { ...defaults, local: 'CUSTOM LOCAL' } }, profiles: [], defaults },
    }, { repoName: 'shop' })
    await flushPromises()

    expect(wrapper.find('.prompt-default-reference').text()).toContain('DEFAULT LOCAL')
    expect(wrapper.vm.promptTemplates.local).toBe('CUSTOM LOCAL')
    await wrapper.find('.prompt-field .link-button').trigger('click')
    expect(wrapper.vm.promptTemplates.local).toBe('DEFAULT LOCAL')
    await wrapper.find('.prompt-default-heading button').trigger('click')
    expect(wrapper.vm.promptTemplates).toEqual(defaults)
    await wrapper.find('[data-testid="api-explanation-settings"] .api-prompt-actions .secondary.sm').trigger('click')
    await flushPromises()
    const saveRequest = api.request.mock.calls.find(([path, options]) => path === '/api/api-explanation-prompts' && options?.method === 'POST')
    expect(saveRequest).toBeTruthy()
    expect(JSON.parse(saveRequest[1].body).templates).toEqual(defaults)
  })

  it('UI-009 saves and tests LLM configuration without displaying the key', async () => {
    const { wrapper, api } = mountUi(LLMSettings, {
      '/api/llm-config': { available: true, model: 'test-model', api_base: 'http://llm.test/v1', api_key_configured: true, disable_ssl_verification: true },
      '/api/llm-config/test': { ok: true, message: '连接成功' },
    }, { open: true })
    await flushPromises()
    expect(wrapper.find('input[type="password"]').element.value).toBe('')
    expect(wrapper.findAll('input[type="checkbox"]')[1].element.checked).toBe(true)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await wrapper.find('.secondary').trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/llm-config/test', { method: 'POST' })
    expect(wrapper.text()).toContain('连接成功')
  })

  it('UI-009 sends a code question and displays audited operations', async () => {
    const { wrapper } = mountUi(RepositoryAssistant, {
      '/api/chat': { answer: '找到调用方', operations: [{ operation: 'codegraph.find_callers', source: 'codegraph', rows: [] }] },
      '/api/audit-logs': { logs: [{ id: 1, status: 'success', question: '谁调用它', plan: [{ operation: 'codegraph.find_callers' }], result_count: 1, duration_ms: 1, created_at: 1 }] },
    }, { repoName: 'shop' })
    await wrapper.find('.assistant-toggle').trigger('click')
    await wrapper.find('textarea').setValue('谁调用 createOrder')
    await wrapper.find('.composer').trigger('submit')
    await flushPromises()
    await wrapper.findAll('.tabs button')[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('codegraph.find_callers')
    expect(wrapper.text()).toContain('谁调用它')
  })

  it('UI-010 records and replays a UI test through the assistant', async () => {
    const recorded = { id: 1, name: '创建商品', status: 'recorded', start_url: 'http://shop.test', steps: [{ action: 'click' }], network_log: [] }
    const { wrapper, api } = mountUi(RepositoryAssistant, {
      '/api/ui-test-targets': { targets: [] },
      '/api/ui-recordings': { recordings: [] },
      '/api/ui-recordings/start': { id: 1, name: '创建商品', status: 'recording', steps: [], network_log: [] },
      '/api/ui-recordings/1/stop': recorded,
      '/api/ui-recordings/1/run': { status: 'passed' },
    }, { repoName: 'shop' })
    await wrapper.find('.assistant-toggle').trigger('click')
    await wrapper.findAll('.tabs button')[2].trigger('click')
    await flushPromises()
    wrapper.vm.targetName = 'shop'; wrapper.vm.targetUrl = 'http://shop.test'; wrapper.vm.testName = '创建商品'
    api.request.mockImplementation(async (path, options) => {
      if (path === '/api/ui-test-targets') return { id: 1, name: 'shop', base_url: 'http://shop.test' }
      if (path === '/api/ui-recordings/start') return { id: 1, name: '创建商品', status: 'recording', steps: [], network_log: [] }
      if (path === '/api/ui-recordings/1/stop') return recorded
      if (path === '/api/ui-recordings/1/run') return { status: 'passed' }
      return {}
    })
    await wrapper.find('.target-form').trigger('submit')
    await flushPromises()
    await wrapper.vm.stopRecording()
    wrapper.vm.recordings = [recorded]
    await wrapper.vm.$nextTick()
    await wrapper.find('.recording-entry button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('passed')
  })

  it('UI-011 adds visible, URL and response checkpoints to a recording', async () => {
    const active = { id: 2, status: 'recording', steps: [] }
    const { wrapper, api } = mountUi(RepositoryAssistant, { '/api/ui-recordings/2/checkpoints': { ...active, steps: [{ action: 'assert-visible' }] } }, { repoName: 'shop' })
    wrapper.vm.activeRecording = active
    wrapper.vm.checkpointType = 'assert-visible'; wrapper.vm.checkpointValue = '保存成功'
    await wrapper.vm.addCheckpoint()
    wrapper.vm.checkpointType = 'assert-url'; wrapper.vm.checkpointValue = '/orders'
    expect(wrapper.vm.buildCheckpoint().payload.value).toBe('/orders')
    wrapper.vm.checkpointType = 'assert-response'; wrapper.vm.checkpointValue = '/api/orders 201'
    expect(wrapper.vm.buildCheckpoint().payload.status).toBe(201)
    expect(api.request).toHaveBeenCalledWith('/api/ui-recordings/2/checkpoints', expect.objectContaining({ method: 'POST' }))
  })

  it('UI-012 preserves successful members in a partial multi-repository run', async () => {
    const { wrapper } = mountUi(Snapshots, {
      '/api/scopes': { scopes: [{ id: 'scope-mall', name: 'mall' }] },
      '/api/scopes/scope-mall/members': { members: [{ id: 'mall', display_name: 'mall' }, { id: 'mall-web', display_name: 'mall-web' }] },
      '/api/repository-members/mall/snapshots': { items: [{ id: 'snap-mall' }] },
      '/api/repository-members/mall-web/snapshots': { items: [] },
    })
    await flushPromises()
    wrapper.vm.setActiveRun({ id: 'run-partial', status: 'partial', members: [
      { member_id: 'mall', attempt: { status: 'completed', stage: 'finished' } },
      { member_id: 'mall-web', attempt: { status: 'failed', stage: 'codegraph_init', error_message: '初始化失败' } },
    ] })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('部分成员未完成')
    expect(wrapper.text()).toContain('初始化失败')
    expect(wrapper.text()).toContain('snap-mall')
  })

  it('UI-013 keeps structural knowledge available when LLM output is disabled', async () => {
    const { wrapper } = mountUi(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [] }, business_descriptions: { note: '请启用 LLM' } },
    }, { repoName: 'shop' })
    await flushPromises()
    expect(wrapper.text()).toContain('API 端点')
    expect(wrapper.text()).toContain('LLM 设置')
  })

  it('UI-014 retries a failed page load and clears the error state', async () => {
    let failed = true
    const { wrapper } = mountUi(Home, {
      '/api/scopes': () => {
        if (failed) { failed = false; throw new Error('network offline') }
        return { scopes: [] }
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('network offline')
    await wrapper.find('.action-btn').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('network offline')
  })

  it('UI-015 keeps top-level Knowledge and Graph View navigation as catalogs', () => {
    const wrapper = mount(App, {
      global: {
        components: { RouterLink },
        stubs: { RepositoryAssistant: true, LLMSettings: true, 'router-view': true },
        mocks: { $route: { params: { repoName: 'shop', viewId: 'view-old' }, query: { snapshot_id: 'snapshot-old' } }, $router: { push: vi.fn() } },
      },
    })
    const links = wrapper.findAll('.nav-links a')
    expect(links[0].attributes('href')).toBe('/knowledge')
    expect(links[2].attributes('href')).toBe('/graph-views')
    expect(links[3].attributes('href')).toBe('/terms')
  })

  it('UI-016 lets users cancel a running Run and retry failed members', async () => {
    const run = { id: 'run-1', status: 'running', members: [{ member_id: 'shop', attempt: { status: 'running', stage: 'analyzing' } }] }
    const { wrapper, api } = mountUi(Snapshots, {
      '/api/scopes': scopeResponse(), '/api/scopes/scope-shop/members': memberResponse(), '/api/repository-members/member-shop/snapshots': { items: [] },
      '/api/analysis-runs/run-1/cancel': { run: { ...run, status: 'cancelled' } },
      '/api/analysis-runs/run-1/retry': { run: { id: 'run-2', status: 'pending', members: [] } },
    })
    await flushPromises()
    wrapper.vm.setActiveRun({ ...run, members: [{ member_id: 'shop', attempt: { status: 'failed', stage: 'analyzing', error_message: 'failed' } }] })
    await wrapper.vm.$nextTick()
    await wrapper.findAll('.run-actions button').find((button) => button.text().includes('重试失败成员')).trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/analysis-runs/run-1/retry', expect.objectContaining({ method: 'POST' }))
  })

  it('UI-017 queries topology, impact and flow from one Graph View', async () => {
    const artifact = { services: [{ member_id: 'shop', display_name: 'shop', entries: [] }, { member_id: 'web', display_name: 'web', entries: [] }], service_projections: [{ source_member_id: 'web', target_member_id: 'shop', kind: 'http', confidence: 'high', evidence: { path: '/api/orders' } }] }
    const { wrapper, api } = mountUi(GraphView, {
      '/api/graph-views/view-1/artifacts/topology': { artifact },
      '/api/graph-views/view-1/impact': { downstream_dependencies: [], upstream_dependents: [] },
      '/api/graph-views/view-1/flow': { nodes: [{ member_id: 'web' }, { member_id: 'shop' }] },
    }, { viewId: 'view-1' })
    await flushPromises()
    expect(wrapper.text()).toContain('调用证据')
    const inputs = wrapper.findAll('.analysis-heading input')
    await inputs[1].setValue('/api/orders')
    await inputs[1].trigger('change')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/graph-views/view-1/flow', { member_id: 'shop', method: 'GET', path: '/api/orders' })
  })

  it('UI-018 checks a member for source changes without creating a Run', async () => {
    const { wrapper, api } = mountUi(Snapshots, {
      '/api/scopes': scopeResponse(),
      '/api/scopes/scope-shop/members': memberResponse(),
      '/api/repository-members/member-shop/snapshots': { items: [{ id: 'snapshot-shop' }] },
      '/api/repository-members/check': { items: [{ member_id: 'member-shop', status: 'source_changed' }] },
    })
    await flushPromises()
    const button = wrapper.findAll('button').find((item) => item.text() === '检查改动')
    await button.trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/repository-members/check', expect.objectContaining({ method: 'POST', body: JSON.stringify({ member_ids: ['member-shop'] }) }))
    expect(api.request).not.toHaveBeenCalledWith('/api/analysis-runs', expect.anything())
    expect(wrapper.find('[data-testid="change-status"]').text()).toContain('现场已变化')
  })
})
