import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Capabilities from '../src/pages/Capabilities.vue'
import Dashboard from '../src/pages/Dashboard.vue'
import EventList from '../src/pages/EventList.vue'
import FeatureDetail from '../src/pages/FeatureDetail.vue'
import FeatureList from '../src/pages/FeatureList.vue'
import Home from '../src/pages/Home.vue'
import Knowledge from '../src/pages/Knowledge.vue'
import Refactoring from '../src/pages/Refactoring.vue'
import RepositoryAssistant from '../src/components/RepositoryAssistant.vue'

const { mermaidRun } = vi.hoisted(() => ({ mermaidRun: vi.fn() }))
vi.mock('mermaid', () => ({ default: { initialize: vi.fn(), run: mermaidRun } }))

const RouterLink = { props: ['to'], template: '<a :href="String(to)"><slot /></a>' }

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
      mocks: { $api: api, $router: { push: vi.fn() }, $runAsync: async (task) => task() },
    },
  })
  return { wrapper, api }
}

afterEach(() => vi.restoreAllMocks())

describe('repository and knowledge pages', () => {
  it('shows refactoring scope, advice and test protection details', async () => {
    const plan = {
      repository_member: 'mall',
      hotspot: { node_id: 'n1', qualified_name: 'OrderService.create', file_path: 'src/order.py', start_line: 10, end_line: 40, score: 18, commit_count: 3, changed_lines: 42 },
      codegraph_impact: { risk: 'MEDIUM', affected_symbol_count: 4, direct_callers: [{ node_id: 'c1', name: 'submit', file_path: 'src/api.py', line: 7 }], direct_callees: [] },
      test_gate: { status: 'missing', refactoring_allowed: false, assessment: 'Static coverage', related_tests: [] },
      agent_task: { title: '建立测试安全网', reason: ['近期反复修改'], instructions: ['只增加测试'], acceptance: ['测试先通过'], test_cases: [{ name: '锁定正常路径', then: '保持返回值' }] },
    }
    const { wrapper, api } = mountPage(Refactoring, {
      '/api/refactor-techniques': { repository_member: 'mall', repository_members: ['mall'], techniques: [{ id: 'extract-method', name: '提取函数', objective: '拆分职责', checks: ['长函数'] }] },
      '/api/refactor-plans': { plans: [plan] },
    }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('OrderService.create')
    expect(wrapper.text()).toContain('src/order.py:10-40')
    expect(wrapper.text()).toContain('缺少关联测试')
    expect(wrapper.text()).toContain('锁定正常路径')
    expect(wrapper.text()).toContain('只增加测试')
    await wrapper.find('.primary').trigger('click')
    expect(api.get).toHaveBeenCalledWith('/api/refactor-plans', expect.objectContaining({ repo: 'mall', member: 'mall', technique: 'extract-method' }))
  })

  it('creates and edits refactoring techniques', async () => {
    const techniques = [{ id: 'extract-method', name: '提取函数', objective: '拆分职责', checks: ['长函数'], source: 'builtin' }]
    const { wrapper, api } = mountPage(Refactoring, {
      '/api/refactor-techniques': { repository_member: 'backend', repository_members: ['backend', 'frontend'], techniques },
      '/api/refactor-plans': { plans: [] },
    }, { repoName: 'mall' })
    await flushPromises()
    await wrapper.find('.header-actions .secondary').trigger('click')
    await wrapper.find('.technique-form input').setValue('domain-check')
    await wrapper.findAll('.technique-form input')[1].setValue('领域检查')
    await wrapper.findAll('.technique-form textarea')[0].setValue('明确领域边界')
    await wrapper.findAll('.technique-form textarea')[1].setValue('逻辑散落\n跨聚合调用')
    api.request.mockResolvedValueOnce({ id: 'domain-check' })
    await wrapper.find('.technique-form').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/refactor-techniques', expect.objectContaining({ query: { repo: 'mall', member: 'backend' }, method: 'POST' }))

    wrapper.vm.techniques = techniques
    wrapper.vm.technique = 'extract-method'
    await wrapper.vm.$nextTick()
    await wrapper.find('.edit-button').trigger('click')
    expect(wrapper.find('.technique-form input').attributes('disabled')).toBeDefined()
    await wrapper.findAll('.technique-form input')[1].setValue('提取小函数')
    api.request.mockResolvedValueOnce({ id: 'extract-method' })
    await wrapper.find('.technique-form').trigger('submit')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/refactor-techniques/extract-method', expect.objectContaining({ query: { repo: 'mall', member: 'backend' }, method: 'PUT' }))
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
      '/api/repos': { repos: [{ name: 'mall', path: '/mall', repositories: [{ name: 'mall' }, { name: 'mall-web' }], stats: { total_commits: 1, active_features: 2, total_events: 3 } }] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('mall-web')
    await wrapper.find('.repo-card').trigger('click')
    expect(wrapper.vm.$router.push).toHaveBeenCalledWith('/repo/mall')
    await wrapper.find('.remove-button').trigger('click')
    expect(api.delete).not.toHaveBeenCalled()
    await wrapper.find('.remove-button').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(api.delete).toHaveBeenCalledWith('/api/repos/mall')
    expect(wrapper.text()).toContain('暂无已注册的代码仓')
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
    await wrapper.find('.detail-heading button').trigger('click')
    const entityButton = wrapper.findAll('.section-nav button').find(button => button.text().includes('核心实体'))
    await entityButton.trigger('click')
    expect(wrapper.text()).toContain('PmsProduct')
    await wrapper.find('.primary').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalled()
    expect(api.get).toHaveBeenLastCalledWith('/api/knowledge', { repo: 'mall', include_llm: true })
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

  it('renders repository and knowledge empty variants', async () => {
    const { wrapper: home } = mountPage(Home, { '/api/repos': { repos: [{ name: 'empty', path: '/empty', repositories: [{ name: 'empty' }], stats: null }] } })
    await flushPromises()
    expect(home.text()).toContain('未分析')

    const { wrapper: knowledge } = mountPage(Knowledge, { '/api/knowledge': null }, { repoName: 'empty' })
    await flushPromises()
    knowledge.vm.report = null
    await knowledge.vm.$nextTick()
    expect(knowledge.text()).toContain('暂无知识数据')
  })
})

describe('evolution dashboard pages', () => {
  it('loads dashboard data and calculates chart widths', async () => {
    const { wrapper } = mountPage(Dashboard, {
      '/api/stats': { total_commits: 4, total_features: 3, active_features: 2, total_events: 1 },
      '/api/event-stats': { stats: [{ event_type: 'BORN', count: 2 }, { event_type: 'DIED', count: 1 }] },
      '/api/events': { events: [{ id: 1, event_type: 'BORN', canonical_name: 'checkout', commit_hash: 'abcdef123', message: 'created' }] },
    }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('checkout')
    expect(wrapper.vm.barWidth(1)).toBe('50%')
    wrapper.vm.eventTypeStats = []
    expect(wrapper.vm.barWidth(1)).toBe('0%')
  })

  it('loads capabilities', async () => {
    const { wrapper } = mountPage(Capabilities, { '/api/capabilities': { capabilities: [{ id: 'c', name: 'Orders', name_zh: '订单', feature_count: 1, event_count: 2, stats: { max_call_depth: 3 }, modules: ['api', 'domain'], features: [{ stable_id: 'f', canonical_name: 'create', entry_signature: 'POST /orders' }] }] } }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('跨模块')
    expect(wrapper.text()).toContain('create')
  })

  it('renders capability and dashboard empty variants', async () => {
    const { wrapper: capabilities } = mountPage(Capabilities, { '/api/capabilities': { capabilities: [{ id: 'single', name: 'Users', name_zh: '', feature_count: 0, event_count: 0, stats: {}, modules: ['users'], module: 'users', features: [] }] } })
    const { wrapper: dashboard } = mountPage(Dashboard, { '/api/stats': {}, '/api/event-stats': { stats: [] }, '/api/events': { events: [] } })
    await flushPromises()
    expect(capabilities.text()).toContain('users')
    expect(dashboard.text()).toContain('暂无数据')
  })

  it('loads, searches and pages features', async () => {
    const { wrapper, api } = mountPage(FeatureList, {
      '/api/commits': { commits: [{ hash: 'abc', timestamp: 1, message: 'init' }] },
      '/api/features': { features: [{ stable_id: 'f', canonical_name: 'checkout', description: 'Checkout flow', description_zh: '结账流程', entry_type: 'http', entry_signature: 'POST /checkout', status: 'active', event_count: 2, call_tree_nodes: 5 }], total: 120 },
    }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('checkout')
    wrapper.vm.searchFeatures(); wrapper.vm.onCommitChange(); wrapper.vm.nextPage(); wrapper.vm.prevPage()
    expect(wrapper.vm.offset).toBe(0)
    expect(wrapper.vm.pageNum).toBe(1)
    expect(wrapper.vm.totalPages).toBe(3)
    expect(wrapper.vm.formatDate(1)).toBeTruthy()
    wrapper.vm.selectedCommit = 'abc'
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('快照')
    await wrapper.find('.clear-btn').trigger('click')
    await wrapper.find('.search-input').setValue('check')
    await wrapper.findAll('select')[1].setValue('active')
    await flushPromises()
    expect(api.get).toHaveBeenCalled()
  })

  it('loads and pages events', async () => {
    const { wrapper } = mountPage(EventList, { '/api/events': { events: [{ id: 1, event_type: 'GROWN', stable_id: 'f', canonical_name: 'checkout', timestamp: 1, commit_hash: 'abc', message: 'grew' }], total: 75 } }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('checkout')
    wrapper.vm.nextPage(); wrapper.vm.prevPage()
    expect(wrapper.vm.pageNum).toBe(1)
    expect(wrapper.vm.totalPages).toBe(2)
    expect(wrapper.vm.formatDate(1)).toBeTruthy()
  })

  it('renders feature and event empty variants', async () => {
    const { wrapper: features } = mountPage(FeatureList, { '/api/commits': { commits: [] }, '/api/features': { features: [], total: 0 } })
    const { wrapper: events } = mountPage(EventList, { '/api/events': { events: [], total: 0 } })
    await flushPromises()
    expect(features.text()).toContain('暂无数据')
    expect(events.text()).toContain('暂无事件数据')
    features.vm.offset = 50; features.vm.prevPage(); features.vm.nextPage()
    events.vm.offset = 50; events.vm.prevPage(); events.vm.nextPage()
    await flushPromises()
  })

  it('renders feature detail, explanation states and mermaid errors', async () => {
    const feature = { stable_id: 'id', canonical_name: 'checkout', description: 'Checkout', description_zh: '结账', entry_type: 'http', entry_signature: 'POST /checkout', status: 'active', event_count: 1, timeline: [{ commit_hash: 'abc', timestamp: 1, event_type: 'BORN', detail: '{"nodes":2}', author: 'dev', message: 'created' }], call_chain: [{ from: 'Checkout.create', to: 'self.validate', depth: 1 }, { from: 'self.validate', to: 'Order.save', depth: 2 }] }
    const { wrapper, api } = mountPage(FeatureDetail, {
      '/api/features/id': feature,
      '/api/llm-status': { available: true },
      '/api/features/id/explain': { available: true, explanation: { zh: '结账流程', en: 'Checkout flow' } },
    }, { repoName: 'mall', stableId: 'id' })
    await flushPromises()
    expect(wrapper.vm.mermaidText).toContain('sequenceDiagram')
    await wrapper.vm.loadExplanation()
    expect(wrapper.vm.explanation.zh).toBe('结账流程')
    expect(wrapper.vm.formatDetail('{"nodes":2}')).toContain('nodes: 2')
    expect(wrapper.vm.formatDetail('plain')).toBe('plain')
    expect(wrapper.vm.formatDate(1)).toBeTruthy()
    mermaidRun.mockRejectedValueOnce(new Error('bad diagram'))
    wrapper.vm.$refs.mermaidEl = document.createElement('div')
    await wrapper.vm.renderDiagram()
    expect(wrapper.vm.diagramError).toBe(true)
    api.get.mockRejectedValueOnce(new Error('offline'))
    await wrapper.vm.loadExplanation()
    expect(wrapper.vm.explainError).toContain('offline')
    api.get.mockResolvedValueOnce({ available: false })
    await wrapper.vm.loadExplanation()
    expect(wrapper.vm.explainError).toContain('OPENAI_API_KEY')
    api.get.mockResolvedValueOnce({ available: true })
    await wrapper.vm.loadExplanation()
    expect(wrapper.vm.explainError).toBe('生成失败')
    expect(wrapper.vm.formatDetail({ nodes: 2 })).toContain('nodes: 2')
    wrapper.vm.feature = { timeline: [], call_chain: [] }
    wrapper.vm.llmAvailable = false
    wrapper.vm.explanation = null
    expect(wrapper.vm.mermaidText).toBe('')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('无内部调用链')
    expect(wrapper.text()).toContain('暂无时间线数据')
    await wrapper.vm.renderDiagram()
  })
})
