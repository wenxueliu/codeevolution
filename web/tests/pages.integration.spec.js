import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'

import App from '../src/App.vue'
import Home from '../src/pages/Home.vue'
import Knowledge from '../src/pages/Knowledge.vue'
import Snapshots from '../src/pages/Snapshots.vue'
import GraphView from '../src/pages/GraphView.vue'
import Terms from '../src/pages/Terms.vue'
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
      if (this.to?.name === 'graph-view') return params.viewId ? `/graph-views/${params.viewId}` : '/graph-views'
      if (this.to?.name === 'knowledge') {
        const snapshotId = this.to?.query?.snapshot_id
        return `/repo/${params.repoName}${snapshotId ? `?snapshot_id=${snapshotId}` : ''}`
      }
      if (this.to?.name === 'knowledge-home') return '/knowledge'
      if (this.to?.name === 'terms') return `/terms${this.to?.query?.view_id ? `?view_id=${this.to.query.view_id}` : ''}`
      return '/'
    },
  },
  template: '<a :href="href"><slot /></a>',
}

function mountPage(component, responses = {}, props = {}, route = { query: { snapshot_id: 'test-snapshot' } }) {
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
      mocks: { $api: api, $router: { push: vi.fn() }, $route: route, $runAsync: async (task) => task() },
    },
  })
  return { wrapper, api }
}

afterEach(() => { vi.restoreAllMocks(); window.sessionStorage.clear() })

describe('repository and knowledge pages', () => {
  it('keeps Knowledge and Graph View discoverable before a context is selected', () => {
    const wrapper = mount(App, {
      global: {
        components: { RouterLink },
        stubs: { RepositoryAssistant: true, LLMSettings: true, 'router-view': true },
        mocks: { $route: { params: {}, query: {} }, $router: { push: vi.fn() } },
      },
    })
    const links = wrapper.findAll('.nav-links a')
    expect(links.map(link => link.text())).toEqual(['知识中心', '快照', '图谱视图'])
    expect(links.map(link => link.attributes('href'))).toEqual(['/knowledge', '/snapshots', '/graph-views'])
  })

  it('renders ranked terms and evidence actions for a snapshot', async () => {
    const { wrapper } = mountPage(Terms, {
      '/api/terms': { total: 1, terms: [{ id: 'term-1', rank: 1, canonical_name: 'Order', term_type: 'entity', confidence_score: 0.96, confidence_band: 'high', status: 'accepted', source: 'rule', aliases: [] }] },
      '/api/terms/term-1/evidence': { evidence: [{ id: 'ev-1', evidence_type: 'api_model_reference', evidence_value: 'POST /orders → Order' }] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('Order')
    expect(wrapper.text()).toContain('96%')
    await wrapper.find('tbody tr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('POST /orders → Order')
  })

  it('renders the Graph View-scoped cross-service alignment workspace', async () => {
    const { wrapper } = mountPage(Terms, {
      '/api/terms/alignments': { view_id: 'view-1', total: 1, alignments: [{ id: 'align-1', source_service_id: 'orders', target_service_id: 'billing', source_term_id: 'term-1', target_term_id: 'term-2', relationship: 'same', confidence: 0.96, status: 'needs_review' }] },
    }, {}, { query: { view_id: 'view-1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('跨服务术语对齐')
    expect(wrapper.text()).toContain('orders')
    expect(wrapper.text()).toContain('96%')
  })

  it('keeps the active snapshot and view context in top-level navigation', () => {
    const wrapper = mount(App, {
      global: {
        components: { RouterLink },
        stubs: { RepositoryAssistant: true, LLMSettings: true, 'router-view': true },
        mocks: { $route: { params: { repoName: 'snapshot', viewId: 'view-1' }, query: { snapshot_id: 'snap-1' } }, $router: { push: vi.fn() } },
      },
    })
    const links = wrapper.findAll('.nav-links a')
    expect(links[0].attributes('href')).toBe('/knowledge')
    expect(links[2].attributes('href')).toBe('/graph-views')
  })

  it('uses persisted current Snapshot and Graph View from the top-level navigation', () => {
    window.sessionStorage.setItem('codeevolution:last-snapshot-id', 'snap-persisted')
    window.sessionStorage.setItem('codeevolution:last-snapshot-member', 'mall')
    window.sessionStorage.setItem('codeevolution:last-graph-view-id', 'view-persisted')
    const wrapper = mount(App, {
      global: {
        components: { RouterLink },
        stubs: { RepositoryAssistant: true, LLMSettings: true, 'router-view': true },
        mocks: { $route: { params: {}, query: {} }, $router: { push: vi.fn() } },
      },
    })
    const links = wrapper.findAll('.nav-links a')
    expect(links[0].attributes('href')).toBe('/knowledge')
    expect(links[2].attributes('href')).toBe('/graph-views')
    wrapper.unmount()
  })

  it('lists every project with a published Snapshot in the Knowledge Center', async () => {
    const { wrapper, api } = mountPage(Knowledge, {
      '/api/scopes': { scopes: [
        { id: 'scope-harness', name: 'harness_framework' },
        { id: 'scope-mall', name: 'mall' },
        { id: 'scope-codeos', name: 'codeos' },
      ] },
      '/api/scopes/scope-harness/members': { members: [{ id: 'member-harness', display_name: 'harness_framework' }] },
      '/api/scopes/scope-mall/members': { members: [{ id: 'member-mall', display_name: 'mall' }] },
      '/api/scopes/scope-codeos/members': { members: [{ id: 'member-codeos', display_name: 'codeos' }] },
      '/api/repository-members/member-harness/snapshots': { items: [{ id: 'snapshot-harness' }] },
      '/api/repository-members/member-mall/snapshots': { items: [{ id: 'snapshot-mall' }] },
      '/api/repository-members/member-codeos/snapshots': { items: [{ id: 'snapshot-codeos' }] },
    }, {}, { query: {} })
    await flushPromises()
    expect(wrapper.find('[data-testid="knowledge-projects"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('harness_framework')
    expect(wrapper.text()).toContain('mall')
    expect(wrapper.text()).toContain('codeos')
    expect(wrapper.findAll('[data-testid="knowledge-project"]').length).toBe(3)
    expect(api.get).not.toHaveBeenCalledWith('/api/knowledge', expect.anything())
  })

  it('loads project knowledge when opening a project from the catalog', async () => {
    const route = reactive({ params: {}, query: {} })
    const { wrapper, api } = mountPage(Knowledge, {
      '/api/scopes': { scopes: [{ id: 'scope-mall', name: 'mall' }] },
      '/api/scopes/scope-mall/members': { members: [{ id: 'member-mall', display_name: 'mall' }] },
      '/api/repository-members/member-mall/snapshots': { items: [{ id: 'snapshot-mall' }] },
      '/api/knowledge': { api_contract: { endpoint_count: 1, endpoints: [] } },
      '/api/business-rules': { rules: [] },
    }, {}, route)
    await flushPromises()
    expect(wrapper.find('[data-testid="knowledge-projects"]').exists()).toBe(true)

    route.query = { snapshot_id: 'snapshot-mall' }
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/knowledge', { snapshot_id: 'snapshot-mall', include_llm: false })
    expect(wrapper.text()).toContain('基于不可变仓库快照推导')
  })

  it('guides the user to Snapshots when Knowledge has no snapshot context', async () => {
    const { wrapper, api } = mountPage(Knowledge, {}, { repoName: '' }, { query: {} })
    await flushPromises()
    expect(wrapper.text()).toContain('还没有已发布的项目知识')
    expect(wrapper.text()).toContain('请先在快照中运行分析')
    expect(wrapper.find('a[href="/snapshots"]').exists()).toBe(true)
    expect(api.get).toHaveBeenCalledWith('/api/scopes')
  })

  it('lists all Graph Views and guides to Snapshots when the catalog is empty', async () => {
    const { wrapper, api } = mountPage(GraphView, {
      '/api/graph-views': { views: [
        { id: 'view-1', lifecycle: 'pinned', completeness: 'complete', members: [{ member_id: 'mall' }] },
        { id: 'view-2', lifecycle: 'ephemeral', completeness: 'incomplete', members: [{ member_id: 'codeos' }] },
      ] },
    }, {}, { query: {} })
    await flushPromises()
    expect(wrapper.find('[data-testid="graph-views"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="graph-view-card"]')).toHaveLength(2)
    expect(wrapper.text()).toContain('view-1')
    expect(wrapper.text()).toContain('view-2')
    expect(api.get).toHaveBeenCalledWith('/api/graph-views')
    wrapper.unmount()
  })

  it('guides the user to Snapshots when the Graph View catalog is empty', async () => {
    const { wrapper, api } = mountPage(GraphView, {}, {}, { query: {} })
    await flushPromises()
    expect(wrapper.text()).toContain('还没有图谱视图')
    expect(wrapper.text()).toContain('请先在快照中选择成员并创建当前视图')
    expect(wrapper.find('a[href="/snapshots"]').exists()).toBe(true)
    expect(api.get).toHaveBeenCalledWith('/api/graph-views')
  })

  it('loads a Graph View topology and keeps view_id when selecting impact and flow', async () => {
    const topology = {
      view_digest: 'sha256:view', coverage: { status: 'complete' },
      services: [
        { member_id: 'mall', snapshot_id: 'snapshot-mall', display_name: 'mall', entries: [{ entry_id: 'mall.root', method: 'GET', path_template: '/api/orders' }] },
        { member_id: 'mall-admin-web', snapshot_id: 'snapshot-web', display_name: 'mall-admin-web', entries: [{ entry_id: 'admin.root', method: 'GET', path_template: '/api/admin' }] },
      ],
      service_projections: [{ source_member_id: 'mall', target_member_id: 'mall-admin-web', kind: 'http', confidence: 'high', evidence: { path: '/api/admin' } }],
    }
    const { wrapper, api } = mountPage(GraphView, {
      '/api/graph-views/view-mall/artifacts/topology': { artifact: topology },
      '/api/graph-views/view-mall/impact': { downstream_dependencies: [{ member_id: 'mall-admin-web', path: ['mall', 'mall-admin-web'] }], upstream_dependents: [] },
      '/api/graph-views/view-mall/flow': { nodes: [{ member_id: 'mall' }, { member_id: 'mall-admin-web' }] },
    }, { viewId: 'view-mall' })
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/graph-views/view-mall/artifacts/topology')
    expect(api.get).toHaveBeenCalledWith('/api/graph-views/view-mall/impact', { member_id: 'mall' })
    expect(wrapper.text()).toContain('mall-admin-web')
    expect(wrapper.text()).toContain('调用证据')
    await wrapper.findAll('.service-list button')[1].trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/graph-views/view-mall/impact', { member_id: 'mall-admin-web' })
    const inputs = wrapper.findAll('.analysis-heading input')
    await inputs[1].setValue('/api/admin')
    await inputs[1].trigger('change')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/graph-views/view-mall/flow', { member_id: 'mall-admin-web', method: 'GET', path: '/api/admin' })
  })

  it('renders a clear empty Graph View state', async () => {
    const { wrapper } = mountPage(GraphView, {
      '/api/graph-views/empty/artifacts/topology': { artifact: { services: [], service_projections: [] } },
    }, { viewId: 'empty' })
    await flushPromises()
    expect(wrapper.text()).toContain('没有可浏览的服务')
  })

  it('loads, saves and tests page-managed LLM configuration without exposing a key', async () => {
    const { wrapper, api } = mountPage(LLMSettings, {
      '/api/llm-config': { available: true, source: 'page', model: 'openai/test', api_base: 'http://llm.test/v1', api_key_configured: true, stored_configured: true, disable_ssl_verification: true },
      '/api/llm-config/test': { ok: true, message: '连接成功', model: 'openai/test' },
    }, { open: true })
    await flushPromises()
    expect(wrapper.text()).toContain('页面配置')
    expect(wrapper.find('input[type="password"]').element.value).toBe('')
    expect(wrapper.findAll('input[type="checkbox"]')[1].element.checked).toBe(true)
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

  it('keeps the add-member input interactive instead of navigating the repository card', async () => {
    const { wrapper, api } = mountPage(Home, {
      '/api/scopes': { scopes: [{ id: 'scope-mall', name: 'mall' }] },
      '/api/scopes/scope-mall/members': { members: [{ id: 'member-mall', display_name: 'mall', registered_path: '/workspace/mall' }] },
    })
    await flushPromises()
    await wrapper.find('[data-testid="add-member"]').trigger('click')
    const input = wrapper.find('[data-testid="member-repository-path"]')
    await input.setValue('/workspace/mall-admin-web')
    expect(input.element.value).toBe('/workspace/mall-admin-web')

    await wrapper.find('[data-testid="confirm-member-repository"]').trigger('click')
    await flushPromises()
    expect(api.request).toHaveBeenCalledWith('/api/scopes/scope-mall/members', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ display_name: 'mall-admin-web', registered_path: '/workspace/mall-admin-web' }),
    }))
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
    await memberCheckboxes[0].setValue(true)
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
    expect(window.sessionStorage.getItem('codeevolution:last-graph-view-id')).toBe('view-1')
    wrapper.unmount()
  })

  it('starts with no members selected and runs only the selected mall member', async () => {
    const { wrapper, api } = mountPage(Snapshots, {
      '/api/scopes': { scopes: [{ id: 'scope-mall', name: 'Mall' }] },
      '/api/scopes/scope-mall/members': { members: [
        { id: 'mall', display_name: 'mall' },
        { id: 'mall-admin-web', display_name: 'mall-admin-web' },
      ] },
      '/api/repository-members/mall/snapshots': { items: [{ id: 'snap-mall' }] },
      '/api/repository-members/mall-admin-web/snapshots': { items: [{ id: 'snap-web' }] },
      '/api/analysis-runs': { run: { id: 'run-mall', status: 'pending', members: [] } },
    })

    await flushPromises()
    const memberCheckboxes = wrapper.findAll('tbody input[type="checkbox"]')
    expect(memberCheckboxes).toHaveLength(2)
    expect(memberCheckboxes.every(({ element }) => !element.checked)).toBe(true)
    expect(window.sessionStorage.getItem('codeevolution:last-snapshot-id')).toBe('snap-mall')
    expect(window.sessionStorage.getItem('codeevolution:last-snapshot-member')).toBe('mall')

    await wrapper.find('input[aria-label="选择 mall"]').setValue(true)
    await wrapper.findAll('.actions button').find(button => button.text() === '运行分析').trigger('click')
    await flushPromises()

    expect(api.request).toHaveBeenCalledWith('/api/analysis-runs', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ member_ids: ['mall'] }),
    }))
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
    const { wrapper, api } = mountPage(Knowledge, {
      '/api/knowledge': report,
      '/api/terms': { total: 1, terms: [{ id: 'term-1', rank: 1, canonical_name: 'Product', term_type: 'entity', confidence_score: 0.98, confidence_band: 'high', status: 'accepted', evidence_count: 2 }] },
    }, { repoName: 'mall' })
    await flushPromises()
    expect(wrapper.text()).toContain('ProductController.create')
    await wrapper.find('tbody tr').trigger('click')
    expect(wrapper.text()).toContain('createProductAPI')
    expect(wrapper.text()).toContain('ProductParam')
    const entityButton = wrapper.findAll('.section-nav button').find(button => button.text().includes('核心实体'))
    await entityButton.trigger('click')
    expect(wrapper.text()).toContain('PmsProduct')
    const termsButton = wrapper.findAll('.section-nav button').find(button => button.text().includes('术语识别'))
    await termsButton.trigger('click')
    expect(wrapper.text()).toContain('Product')
    expect(wrapper.text()).toContain('98%')
    await wrapper.find('.primary').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalled()
    expect(api.get).toHaveBeenCalledWith('/api/knowledge', { snapshot_id: 'test-snapshot', include_llm: true })
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

  it('prefills API explanation settings with the system defaults', async () => {
    const { wrapper } = mountPage(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 0, endpoints: [] } },
      '/api/api-explanation-prompts': {
        current: null,
        profiles: [],
        default_guidance: '系统默认 API 分析指导',
        defaults: { local: '默认自身模板', synthesis: '默认合并模板', aggregate: '默认聚合模板' },
      },
      '/api/api-explanations/batches': { batches: [] },
    }, { repoName: 'mall' })
    await flushPromises()

    const textareas = wrapper.find('[data-testid="api-explanation-settings"]').findAll('textarea')
    expect(textareas.map(({ element }) => element.value)).toEqual([
      '系统默认 API 分析指导', '默认自身模板', '默认合并模板', '默认聚合模板',
    ])
    expect(wrapper.find('[data-testid="api-explanation-settings"]').text()).toContain('恢复全部默认')
  })

  it('does not let empty current prompt templates hide system defaults', async () => {
    const { wrapper } = mountPage(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 0, endpoints: [] } },
      '/api/api-explanation-prompts': {
        current: { prompt_text: '', prompt_templates: { local: '', synthesis: '', aggregate: '' } },
        profiles: [],
        default_guidance: '系统默认 API 分析指导',
        defaults: { local: '默认自身模板', synthesis: '默认合并模板', aggregate: '默认聚合模板' },
      },
      '/api/api-explanations/batches': { batches: [] },
    }, { repoName: 'mall' })
    await flushPromises()

    expect(wrapper.vm.promptTemplates).toEqual({
      local: '默认自身模板', synthesis: '默认合并模板', aggregate: '默认聚合模板',
    })
  })

  it('keeps editable fallback templates and enables restore when prompt loading fails', async () => {
    const { wrapper } = mountPage(Knowledge, {
      '/api/knowledge': { api_contract: { endpoint_count: 0, endpoints: [] } },
      '/api/api-explanation-prompts': () => { throw new Error('prompt endpoint unavailable') },
      '/api/api-explanations/batches': { batches: [] },
    }, { repoName: 'mall' })
    await flushPromises()

    const panel = wrapper.find('[data-testid="api-explanation-settings"]')
    const textareas = panel.findAll('textarea')
    expect(textareas).toHaveLength(4)
    expect(textareas.slice(1).every(({ element }) => element.value.length > 0)).toBe(true)
    expect(panel.find('.prompt-default-heading button').element.disabled).toBe(false)
    expect(panel.find('.explanation-error').text()).toContain('prompt endpoint unavailable')
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
