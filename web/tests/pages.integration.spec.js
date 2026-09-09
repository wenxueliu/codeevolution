import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Home from '../src/pages/Home.vue'
import Knowledge from '../src/pages/Knowledge.vue'
import Snapshots from '../src/pages/Snapshots.vue'
import GraphView from '../src/pages/GraphView.vue'
import RepositoryAssistant from '../src/components/RepositoryAssistant.vue'
import LLMSettings from '../src/components/LLMSettings.vue'

const { mermaidRun } = vi.hoisted(() => ({ mermaidRun: vi.fn() }))
vi.mock('mermaid', () => ({ default: { initialize: vi.fn(), run: mermaidRun } }))

const RouterLink = {
  props: ['to'],
  computed: {
    href() {
      if (typeof this.to === 'string') return this.to
      const params = this.to?.params || {}
      if (this.to?.name === 'snapshots') return '/snapshots'
      if (this.to?.name === 'graph-view') return `/graph-views/${params.viewId}`
      if (this.to?.name === 'knowledge') return `/repo/${params.repoName}`
      return '/'
    },
  },
  template: '<a :href="href"><slot /></a>',
}

function mountPage(component, responses = {}, props = {}) {
  const api = {
    get: vi.fn(async (path) => typeof responses[path] === 'function' ? responses[path]() : responses[path] ?? {}),
    delete: vi.fn(async () => ({ ok: true })),
    request: vi.fn(async (path, options) => typeof responses[path] === 'function' ? responses[path](options) : responses[path] ?? {}),
  }
  const wrapper = mount(component, {
    props,
    global: {
      components: { RouterLink },
      mixins: [{ data: () => ({ loading: false, error: null }) }],
      mocks: { $api: api, $router: { push: vi.fn() }, $route: { query: { snapshot_id: 'test-snapshot' } }, $runAsync: async (task) => task() },
    },
  })
  return { wrapper, api }
}

afterEach(() => { vi.restoreAllMocks(); window.sessionStorage.clear() })

describe('repository and knowledge pages', () => {
  it('loads a Graph View topology and keeps view_id when selecting impact and flow', async () => {
    const topology = {
      view_id: 'view-mall', completeness: 'complete',
      services: [
        { member_id: 'mall', snapshot_id: 'snapshot-mall', endpoints: [{ path: '/api/orders' }] },
        { member_id: 'mall-admin-web', snapshot_id: 'snapshot-web', endpoints: [{ path: '/api/admin' }] },
      ],
      edges: [{ source_member_id: 'mall', target_member_id: 'mall-admin-web', kind: 'http', confidence: 'exact', source_endpoint: '/api/orders', target_endpoint: '/api/admin', evidence: { path: '/api/admin', file: 'OrderService.java', line: 42 } }],
    }
    const { wrapper, api } = mountPage(GraphView, {
      '/api/topology': topology,
      '/api/impact': { affected: [{ member_id: 'mall' }, { member_id: 'mall-admin-web' }], edges: topology.edges },
      '/api/flow': { steps: [{ member_id: 'mall' }, { member_id: 'mall-admin-web' }] },
    }, { viewId: 'view-mall' })
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/topology', { view_id: 'view-mall' })
    expect(api.get).toHaveBeenCalledWith('/api/impact', { view_id: 'view-mall', service: 'mall' })
    expect(api.get).toHaveBeenCalledWith('/api/flow', { view_id: 'view-mall', service: 'mall', path: '' })
    expect(wrapper.text()).toContain('mall-admin-web')
    expect(wrapper.text()).toContain('调用证据')
    await wrapper.findAll('.service-list button')[1].trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/impact', { view_id: 'view-mall', service: 'mall-admin-web' })
    await wrapper.find('.analysis-heading input').setValue('/api/admin')
    await wrapper.find('.analysis-heading input').trigger('change')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/flow', { view_id: 'view-mall', service: 'mall-admin-web', path: '/api/admin' })
  })

  it('renders a clear empty Graph View state', async () => {
    const { wrapper } = mountPage(GraphView, {
      '/api/topology': { view_id: 'empty', services: [], edges: [] },
    }, { viewId: 'empty' })
    await flushPromises()
    expect(wrapper.text()).toContain('没有可浏览的服务')
  })

  it('loads, saves and tests page-managed LLM configuration without exposing a key', async () => {
    const { wrapper, api } = mountPage(LLMSettings, {
      '/api/llm-config': { available: true, source: 'page', model: 'openai/test', api_base: 'https://llm.test/v1', api_key_configured: true, stored_configured: true },
      '/api/llm-config/test': { ok: true, message: '连接成功', model: 'openai/test' },
    }, { open: true })
    await flushPromises()
    expect(wrapper.text()).toContain('页面配置')
    expect(wrapper.find('input[type="password"]').element.value).toBe('')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/llm-config', expect.objectContaining({ method: 'PUT' }))
    await wrapper.find('.secondary').trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/llm-config/test', { method: 'POST' })
    expect(wrapper.text()).toContain('连接成功')
  })

  it('opens repository assistant, executes a question and renders audit logs', async () => {
    const { wrapper, api } = mountPage(RepositoryAssistant, {
      '/api/chat': { answer: '查询到调用方', operations: [{ operation: 'codegraph.find_callers', source: 'codegraph', rows: [{ name: 'checkout' }] }] },
      '/api/audit-logs': { logs: [{ id: 1, status: 'success', question: '谁调用它', plan: [{ operation: 'codegraph.find_callers' }], result_count: 1, duration_ms: 2, created_at: 1 }] },
    }, { repoName: 'mall' })
    await wrapper.find('.assistant-toggle').trigger('click')
    await wrapper.find('textarea').setValue('谁调用 createOrder')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/chat', expect.objectContaining({ method: 'POST' }))
    expect(wrapper.text()).toContain('查询到调用方')
    expect(wrapper.text()).toContain('codegraph.find_callers')
    await wrapper.findAll('.tabs button')[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('谁调用它')
  })

  it('registers an external target, records steps and replays through WebBridge APIs', async () => {
    const recording = { id: 7, name: '创建商品', status: 'recording', start_url: 'http://shop.test/add', steps: [], network_log: [] }
    const recorded = { ...recording, status: 'recorded', steps: [{ action: 'click', target: { role: 'button', name: '保存' } }], network_log: [{ path: '/api/product' }] }
    const { wrapper, api } = mountPage(RepositoryAssistant, {
      '/api/ui-test-targets': { targets: [] },
      '/api/ui-recordings': { recordings: [] },
      '/api/ui-recordings/start': recording,
      '/api/ui-recordings/7/collect': recording,
      '/api/ui-recordings/7/stop': recorded,
      '/api/ui-recordings/7/run': { id: 2, status: 'passed' },
    }, { repoName: 'mall' })
    await wrapper.find('.assistant-toggle').trigger('click')
    await wrapper.findAll('.tabs button')[2].trigger('click')
    await flushPromises()
    wrapper.vm.targetName = 'shop'; wrapper.vm.targetUrl = 'http://shop.test/add'; wrapper.vm.testName = '创建商品'
    api.request.mockImplementationOnce(async () => ({ id: 3, name: 'shop', base_url: 'http://shop.test' }))
    await wrapper.find('.target-form').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/ui-recordings/start', expect.objectContaining({ method: 'POST' }))
    wrapper.vm.activeRecording = recording
    await wrapper.vm.stopRecording()
    wrapper.vm.recordings = [recorded]
    await wrapper.vm.$nextTick()
    await wrapper.find('.recording-entry button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('passed')
    wrapper.unmount()
  })

  it('handles active recordings, target updates and recorder request failures', async () => {
    const active = { id: 8, name: '编辑商品', status: 'recording', steps: [] }
    const { wrapper, api } = mountPage(RepositoryAssistant, {
      '/api/ui-test-targets': { targets: [{ id: 4, name: 'shop' }] },
      '/api/ui-recordings': { recordings: [active] },
      '/api/ui-recordings/start': active,
    }, { repoName: 'mall' })
    wrapper.vm.startCollectTimer = vi.fn()
    await wrapper.vm.showUiTests()
    expect(wrapper.vm.activeRecording.id).toBe(8)
    wrapper.vm.targetName = 'shop'; wrapper.vm.targetUrl = 'http://shop.test'; wrapper.vm.testName = '编辑商品'
    await wrapper.vm.startRecording()
    expect(api.request).toHaveBeenCalledWith('/api/ui-test-targets', expect.objectContaining({ method: 'POST' }))

    wrapper.vm.activeRecording = null
    await wrapper.vm.collectRecording(); await wrapper.vm.stopRecording()
    wrapper.vm.activeRecording = active
    api.request.mockRejectedValue(new Error('bridge offline'))
    await wrapper.vm.collectRecording()
    expect(wrapper.vm.uiError).toContain('bridge offline')
    await wrapper.vm.stopRecording()
    expect(wrapper.vm.uiBusy).toBe(false)
    await wrapper.vm.runRecording({ id: 8 })
    expect(wrapper.vm.uiError).toContain('bridge offline')
    wrapper.unmount()
  })

  it('builds and saves phase-two UI checkpoints', async () => {
    const active = { id: 9, status: 'recording', steps: [] }
    const { wrapper, api } = mountPage(RepositoryAssistant, {
      '/api/ui-recordings/9/checkpoints': { ...active, steps: [{ action: 'assert-visible' }] },
    }, { repoName: 'mall' })
    wrapper.vm.activeRecording = active
    wrapper.vm.checkpointType = 'assert-visible'; wrapper.vm.checkpointValue = '保存成功'
    expect(wrapper.vm.checkpointPlaceholder).toContain('文本')
    await wrapper.vm.addCheckpoint()
    expect(api.request).toHaveBeenCalledWith('/api/ui-recordings/9/checkpoints', expect.objectContaining({ method: 'POST' }))
    wrapper.vm.checkpointType = 'assert-url'; wrapper.vm.checkpointValue = '/orders'
    expect(wrapper.vm.buildCheckpoint().payload.value).toBe('/orders')
    wrapper.vm.checkpointType = 'assert-response'; wrapper.vm.checkpointValue = '/api/orders 201'
    expect(wrapper.vm.buildCheckpoint().payload.status).toBe(201)
    wrapper.vm.checkpointType = 'fixture'; wrapper.vm.checkpointValue = '{"url":"http://shop.test/reset","method":"POST"}'
    expect(wrapper.vm.checkpointPlaceholder).toContain('JSON')
    expect(wrapper.vm.buildCheckpoint().payload.method).toBe('POST')
    wrapper.unmount()
  })

  it('loads grouped repositories, navigates, cancels and confirms removal', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    const { wrapper, api } = mountPage(Home, {
      '/api/scopes': { scopes: [{ id: 'scope-mall', name: 'mall' }] },
      '/api/scopes/scope-mall/members': { members: [{ id: 'member-mall', display_name: 'mall', registered_path: '/mall' }, { id: 'member-web', display_name: 'mall-web', registered_path: '/mall-web' }] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('mall-web')
    expect(wrapper.find('.repo-link').attributes('href')).toBe('/snapshots')
    await wrapper.find('.remove-button').trigger('click')
    expect(api.delete).not.toHaveBeenCalled()
    await wrapper.find('.remove-button').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(api.delete).toHaveBeenCalledWith('/api/scopes/scope-mall')
    expect(wrapper.text()).toContain('还没有代码仓')
  })

  it('registers a repository from the empty-state workflow', async () => {
    const { wrapper, api } = mountPage(Home, { '/api/scopes': { scopes: [] } })
    await flushPromises()
    await wrapper.find('.empty-state button').trigger('click')
    await wrapper.findAll('.register-form input')[0].setValue('shop')
    await wrapper.findAll('.register-form input')[1].setValue('/workspace/shop')
    api.request.mockImplementation(async (path) => {
      if (path === '/api/scopes') return { scope: { id: 'scope-shop', name: 'shop' } }
      return { member: { id: 'member-shop' } }
    })
    await wrapper.find('.register-form').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/scopes', expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'shop' }) }))
    expect(api.request).toHaveBeenCalledWith('/api/scopes/scope-shop/members', expect.objectContaining({ method: 'POST', body: JSON.stringify({ display_name: 'shop', registered_path: '/workspace/shop' }) }))
  })

  it('selects scope members and follows an analysis run through cancel and failed-member retry', async () => {
    const runningRun = {
      id: 'run-1', status: 'running', members: [
        { member_id: 'mall', disposition: 'queued', attempt: { status: 'running', stage: 'codegraph_sync', progress: { percent: 60 } } },
      ],
    }
    const failedRun = {
      id: 'run-1', status: 'partial', members: [
        { member_id: 'mall', disposition: 'queued', attempt: { status: 'completed', stage: 'finished' } },
        { member_id: 'mall-admin-web', disposition: 'queued', attempt: { status: 'failed', stage: 'codegraph_init', error_message: 'codegraph unavailable' } },
      ],
    }
    const { wrapper, api } = mountPage(Snapshots, {
      '/api/scopes': { scopes: [{ id: 'scope-mall', name: 'Mall' }] },
      '/api/scopes/scope-mall/members': { members: [{ id: 'mall', display_name: 'mall' }, { id: 'mall-admin-web', display_name: 'mall-admin-web' }] },
      '/api/repository-members/mall/snapshots': { items: [{ id: 'snap-mall' }] },
      '/api/repository-members/mall-admin-web/snapshots': { items: [] },
      '/api/analysis-runs': { run: runningRun },
      '/api/analysis-runs/run-1/cancel': { run: runningRun },
      '/api/analysis-runs/run-1/retry': { run: { id: 'run-2', status: 'pending', members: [{ member_id: 'mall-admin-web', disposition: 'queued', attempt: { status: 'pending', stage: 'queued' } }] } },
      '/api/graph-views/current': { view: { id: 'view-1', digest: 'digest-1' } },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('mall-admin-web')
    const memberCheckboxes = wrapper.findAll('tbody input[type="checkbox"]')
    await memberCheckboxes[1].setValue(false)
    await wrapper.findAll('.actions button')[1].trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/analysis-runs', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ member_ids: ['mall'] }),
    }))
    expect(wrapper.find('[data-testid="analysis-run"]').text()).toContain('同步 CodeGraph')
    expect(window.sessionStorage.getItem('codeevolution:last-analysis-run-id')).toBe('run-1')

    await wrapper.find('.run-actions button').trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/analysis-runs/run-1/cancel', { method: 'POST' })

    wrapper.vm.setActiveRun(failedRun)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('codegraph unavailable')
    await wrapper.findAll('.run-actions button').find(button => button.text().includes('重试失败成员')).trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/analysis-runs/run-1/retry', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ member_ids: ['mall-admin-web'] }),
    }))
    expect(wrapper.text()).toContain('run-2')

    await wrapper.vm.createView()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('查看跨仓图谱')
    wrapper.unmount()
  })

  it('renders contract details, domain entities, sections and explicit LLM loading', async () => {
    const report = {
      api_contract: { endpoint_count: 1, endpoints: [{ method: 'POST', path: '/product/create', handler: 'ProductController.create', request_headers: [{ name: 'Authorization' }], path_params: [], query_params: [], request_body: { type: 'ProductParam' }, response_body: { type: 'CommonResult<Product>' }, call_chain: [{ id: '1', name: 'create' }, { id: '2', name: 'save' }], frontend_callers: [{ function: 'createProductAPI', definition_file: 'src/apis/product.ts', definition_line: 10, call_sites: [{ file: 'src/views/product.vue', line: 20 }] }] }] },
      module_topology: { module_count: 1, coupling_score: 0.2, modules: [{ id: 'm', name: 'product', file_count: 3, primary_language: 'java' }] },
      core_entities: [{ qualified_name: 'PmsProduct', name: 'PmsProduct', kind: 'class', field_count: 20, relationship_count: 4, score: 25, repository: 'mall', file_path: 'model/PmsProduct.java' }],
      test_coverage: { coverage_pct: 75, gap_count: 1, top_gaps: [{ qualified_name: 'save', kind: 'method', file_path: 'service.java', line: 2 }] },
      layer_violations: { violation_count: 1, violations: [{ source_layer: 'api', source_file: 'a', target_layer: 'infra', target_file: 'b' }] },
      config_consumption: { files: [] }, external_dependencies: {}, authorization_model: {}, heat_map: {},
      business_descriptions: { note: 'disabled' }, business_rules: { note: 'disabled' }, error_catalog: { note: 'disabled' }, state_machines: { note: 'disabled' },
    }
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { wrapper, api } = mountPage(Knowledge, { '/api/knowledge': report }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('ProductController.create')
    await wrapper.find('tbody tr').trigger('click')
    expect(wrapper.text()).toContain('createProductAPI')
    expect(wrapper.text()).toContain('ProductParam')
    const entityButton = wrapper.findAll('.section-nav button').find(button => button.text().includes('核心实体'))
    await entityButton.trigger('click')
    expect(wrapper.text()).toContain('PmsProduct')
    await wrapper.find('.primary').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalled()
    expect(api.get).toHaveBeenLastCalledWith('/api/knowledge', { snapshot_id: 'test-snapshot', include_llm: true })
    wrapper.vm.activeSection = 'business_descriptions'
    await wrapper.vm.$nextTick()
    expect(wrapper.vm.isDisabled({ note: 'x' })).toBe(true)
    expect(wrapper.vm.formatJson({ ok: true })).toContain('"ok"')

    for (const key of ['module_topology', 'test_coverage', 'layer_violations', 'config_consumption']) {
      wrapper.vm.activeSection = key
      await wrapper.vm.$nextTick()
      expect(wrapper.find('.panel').exists()).toBe(true)
    }
    wrapper.vm.report.business_descriptions = { note: '请启用 LLM' }
    wrapper.vm.activeSection = 'business_descriptions'
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('现在抽取')
  })

  it('enlarges and zooms the sequence diagram for each API endpoint', async () => {
    mermaidRun.mockClear()
    const endpoints = [
      { method: 'GET', path: '/first', handler: 'FirstHandler', call_chain_mermaid: 'sequenceDiagram\nA->>B: FirstHandler' },
      { method: 'POST', path: '/second', handler: 'SecondHandler', call_chain_mermaid: 'sequenceDiagram\nC->>D: SecondHandler' },
      { method: 'DELETE', path: '/without-sequence', handler: 'ThirdHandler' },
    ]
    const { wrapper } = mountPage(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: endpoints.length, endpoints } },
    }, { repoName: 'mall' })
    await flushPromises()

    for (const row of wrapper.findAll('tr.clickable').slice(0, 2)) await row.trigger('click')
    expect(wrapper.findAll('[data-testid="sequence-expand"]')).toHaveLength(2)

    await wrapper.findAll('[data-testid="sequence-expand"]')[1].trigger('click')
    await flushPromises()
    const dialog = wrapper.find('[data-testid="sequence-dialog"]')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.text()).toContain('POST')
    expect(dialog.text()).toContain('/second')
    expect(dialog.find('.mermaid').text()).toContain('SecondHandler')
    expect(dialog.find('.mermaid').text()).not.toContain('FirstHandler')
    expect(mermaidRun).toHaveBeenCalledWith({ nodes: [expect.any(HTMLElement)] })

    await dialog.find('[aria-label="放大时序图"]').trigger('click')
    expect(dialog.text()).toContain('125%')
    await dialog.findAll('.sequence-zoom-controls button')[2].trigger('click')
    expect(dialog.text()).toContain('100%')
    await dialog.find('.sequence-zoom-close').trigger('click')
    expect(wrapper.find('[data-testid="sequence-dialog"]').exists()).toBe(false)
  })

  it('loads endpoint explanations without generating, then manually generates, polls and deletes snapshots', async () => {
    vi.useFakeTimers()
    const endpoint = { method: 'POST', path: '/orders', handler: 'OrderController.create', repository: 'orders', file: 'OrderController.java', line: 42 }
    let pollCompleted = false
    const completed = {
      id: 'snapshot-2', status: 'completed', source_revision: '1234567890abcdef', model_id: 'test-model', created_at: 1,
      explanation: { summary: '创建订单并预占库存', main_flow: ['校验请求', { title: '预占库存', detail: '调用库存服务' }] },
      coverage: { total_nodes: 2, completed_nodes: 2, partial_nodes: 0, failed_nodes: 0 },
      nodes: [
        { node_key: 'orders::OrderService.create', status: 'completed', file: 'OrderService.java', line_start: 10, local_explanation: { summary: '协调订单创建' } },
        { node_key: 'orders::StockService.reserve', status: 'completed', local_explanation: { summary: '预占库存' } },
      ],
    }
    const { wrapper, api } = mountPage(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [endpoint] } },
      '/api/api-explanations/current': () => pollCompleted ? { snapshot: completed } : { status: 'missing' },
      '/api/api-explanations/snapshots': () => ({ snapshots: pollCompleted ? [completed] : [] }),
      '/api/api-explanations/generate': { id: 'snapshot-2', status: 'running', created_at: 1 },
    }, { repoName: 'mall' })
    await flushPromises()

    expect(api.request).not.toHaveBeenCalledWith('/api/api-explanations/generate', expect.anything())
    await wrapper.find('tr.clickable').trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/api-explanations/current', { repository_snapshot_id: 'test-snapshot', api_key: 'POST|/orders|OrderController.create' })
    expect(wrapper.text()).toContain('尚未生成该端点的解释快照')

    await wrapper.find('[data-testid="explanation-generate"]').trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/api-explanations/generate', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ repository_snapshot_id: 'test-snapshot', repo: 'mall', member: 'orders', method: 'POST', path: '/orders', handler: 'OrderController.create', file: 'OrderController.java', line: 42 }),
    }))
    expect(wrapper.text()).toContain('正在生成候选快照')

    pollCompleted = true
    await vi.runOnlyPendingTimersAsync()
    await flushPromises()
    expect(wrapper.text()).toContain('创建订单并预占库存')
    expect(wrapper.text()).toContain('节点 2/2')
    expect(wrapper.text()).toContain('节点解释状态（2）')

    await wrapper.findAll('.api-explanation-actions button')[1].trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="explanation-snapshots"]').text()).toContain('snapshot-2')
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await wrapper.find('.snapshot-item button').trigger('click')
    await flushPromises()
    expect(api.delete).toHaveBeenCalledWith('/api/api-explanations/snapshots/snapshot-2?confirm_current=true')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('filters API contracts by service and search while paginating the result set', async () => {
    const endpoints = Array.from({ length: 60 }, (_, index) => ({
      method: index % 2 ? 'POST' : 'GET',
      path: `/orders/${index}`,
      handler: `handler${index}`,
      repository: index % 3 ? 'orders' : 'billing',
    }))
    const { wrapper } = mountPage(Knowledge, { '/api/knowledge': { api_contract: { endpoint_count: 60, endpoints } } }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.findAll('tbody tr')).toHaveLength(25)

    const serviceSelect = wrapper.find('[data-testid="endpoint-service-filter"]')
    expect(serviceSelect.findAll('option').map(option => option.text())).toEqual(['全部服务', 'billing', 'orders'])
    await serviceSelect.setValue('billing')
    expect(wrapper.findAll('tbody tr')).toHaveLength(20)
    expect(wrapper.text()).toContain('/orders/0')
    expect(wrapper.findAll('tbody tr code').some(node => node.text() === '/orders/1')).toBe(false)

    await wrapper.find('.table-tools input').setValue('/orders/59')
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)

    await serviceSelect.setValue('orders')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.text()).toContain('/orders/59')
  })

  it('renders repository and knowledge empty variants', async () => {
    const { wrapper: home } = mountPage(Home, { '/api/scopes': { scopes: [{ id: 'scope-empty', name: 'empty' }] }, '/api/scopes/scope-empty/members': { members: [] } })
    await flushPromises()
    expect(home.text()).toContain('查看 Snapshots')

    const { wrapper: knowledge } = mountPage(Knowledge, { '/api/knowledge': null }, { repoName: 'empty' })
    await flushPromises()
    knowledge.vm.report = null
    await knowledge.vm.$nextTick()
    expect(knowledge.text()).toContain('暂无知识数据')
  })
})
