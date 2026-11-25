import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import Knowledge from '../src/pages/Knowledge.vue'

const BACKTEST = {
  status: 'ready', verdict: 'partial', confidence: 0.66,
  reasons: ['代码里仍保留旧的 Node 网关入口', 'Rust 网关已经承接主流量'],
  evidence: [{ path: 'src/gateway/main.rs', line: 12, note: 'Rust 网关启动入口' }],
  recommendation: '删除旧 Node 网关入口后再把状态改为 accepted。',
  error_message: '', duration_ms: 840, created_at: '2026-11-25T00:00:00Z',
}

function draftFixture(overrides = {}) {
  return {
    id: 'd1', title: '测试 ADR', slug: 'test-adr', status: 'proposed',
    review_status: 'pending', rating: null, review_comment: '', reviewed_at: null,
    markdown: '# 测试 ADR\n', exported_path: '', created_at: '2026-11-25T00:00:00Z',
    source_kind: 'text', source_ref: 'abc', backtest: { verdict: 'partial', confidence: 0.66, status: 'ready' },
    ...overrides,
  }
}

const GENERATION = {
  id: 'g1', status: 'succeeded', stage: 'draft_ready', progress: 100,
  source_kind: 'text', source_ref: 'abc', source_payload: { text: { text: '决定' } },
  source_digest: 'sha256:s', prompt_id: '', prompt_version: 0, prompt_digest: 'sha256:p',
  prompt_text: 'text {{text}}', prompt_rendered: 'text 决定', model: 'fake-model',
  raw_response: '{"is_decision": true}', parsed: { is_decision: true }, error_message: '',
  skip_reason: '', draft_id: 'd1', duration_ms: 12, created_at: '2026-11-25T00:00:00Z',
}

function commitItems() {
  return [
    {
      sha: 'sha-feat', subject: 'feat: add payment gateway', author: 'alice',
      committed_at: '2026-11-20T10:00:00Z', files: ['src/gateway.rs', 'src/router.rs'],
      rule_candidate: true, ai_candidate: true, candidate: true, skip_reason: '',
      change_type: 'feature', scanned: true, stage: 'pending',
      generation_id: '', generation_status: '', generation_stage: '', generation_progress: 0,
      draft_id: '', draft_title: '', draft_status: '', review_status: '', rating: null, exported_path: '',
      backtest: null,
    },
    {
      sha: 'sha-docs', subject: 'docs: update readme', author: 'bob',
      committed_at: '2026-11-19T10:00:00Z', files: ['README.md'],
      rule_candidate: false, ai_candidate: false, candidate: false,
      skip_reason: '仅修改文档、测试或 lockfile', change_type: 'docs', scanned: true, stage: 'skipped',
      generation_id: '', generation_status: '', generation_stage: '', generation_progress: 0,
      draft_id: '', draft_title: '', draft_status: '', review_status: '', rating: null, exported_path: '',
      backtest: null,
    },
    {
      sha: 'sha-pending', subject: 'chore: wire config', author: 'carol',
      committed_at: '2026-11-18T10:00:00Z', files: ['config/app.yaml'],
      rule_candidate: true, ai_candidate: null, candidate: true, skip_reason: '',
      change_type: '', scanned: false, stage: 'pending',
      generation_id: '', generation_status: '', generation_stage: '', generation_progress: 0,
      draft_id: '', draft_title: '', draft_status: '', review_status: '', rating: null, exported_path: '',
      backtest: null,
    },
    {
      sha: 'sha-generated', subject: 'feat: add retry budget', author: 'dave',
      committed_at: '2026-11-17T10:00:00Z', files: ['src/retry.rs'],
      rule_candidate: true, ai_candidate: true, candidate: true, skip_reason: '',
      change_type: 'feature', scanned: true, stage: 'generated',
      generation_id: 'g1', generation_status: 'succeeded', generation_stage: 'draft_ready',
      generation_progress: 100, draft_id: 'd1', draft_title: '测试 ADR', draft_status: 'proposed',
      review_status: 'pending', rating: null, exported_path: '',
      backtest: null,
    },
  ]
}

const COMMIT_COUNTS = {
  pending: 2, queued: 0, generating: 0, generated: 1, backtested: 0,
  approved: 0, rejected: 0, exported: 0, skipped: 1, failed: 0,
}

const COMMIT_PROGRESS = { total: 4, processed: 1, active: 0, percent: 25 }

function mountKnowledge() {
  const posted = []
  let drafts = [draftFixture()]
  let template = {
    member_id: 'member-1', template: null,
    default_template: '# {{title}}\n\n## Context\n\n{{context}}\n',
    template_digest: 'sha256:default', template_version: 0,
  }

  const commitListing = () => ({
    member_id: 'member-1', repo: '/repo', total: 4, scanned: 2, stage: '',
    counts: { ...COMMIT_COUNTS }, progress: { ...COMMIT_PROGRESS }, items: commitItems(),
  })

  const api = {
    get: vi.fn(async (path, query = {}) => {
      if (path === '/api/knowledge') {
        return { adr: { available: false, records: [], violations: [] } }
      }
      if (path === '/api/adr/prompts') {
        return {
          member_id: 'member-1',
          kinds: [
            { source_kind: 'commit', prompt: null, default_prompt: 'commit {{diff}}', prompt_digest: 'sha256:a' },
            { source_kind: 'file', prompt: null, default_prompt: 'file {{content}}', prompt_digest: 'sha256:b' },
            { source_kind: 'text', prompt: null, default_prompt: 'text {{text}}', prompt_digest: 'sha256:c' },
            { source_kind: 'triage', prompt: null, default_prompt: 'triage {{commits}}', prompt_digest: 'sha256:d' },
            { source_kind: 'backtest', prompt: null, default_prompt: 'backtest {{adr}}', prompt_digest: 'sha256:e' },
          ],
        }
      }
      if (path === '/api/adr/commits') {
        const listing = commitListing()
        if (query.stage) {
          return { ...listing, stage: query.stage, items: listing.items.filter(item => item.stage === query.stage) }
        }
        return listing
      }
      if (path === '/api/adr/template') return template
      if (path === '/api/adr/drafts') return { drafts }
      if (path === '/api/adr/drafts/d1') {
        return { draft: drafts[0], generation: GENERATION, backtest: BACKTEST }
      }
      if (path === '/api/adr/generations/g1') {
        return { generation: { ...GENERATION, status: 'running', stage: 'calling_model', progress: 40 } }
      }
      if (path === '/api/adr/generations/g2') {
        return { generation: GENERATION, draft: drafts[0] }
      }
      return { rules: [] }
    }),
    request: vi.fn(async (path, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null
      posted.push({ path, body })
      if (path === '/api/adr/generations') {
        return { generation: { id: 'g2', status: 'running', stage: 'calling_model', progress: 10, source_kind: body?.source?.kind } }
      }
      if (path === '/api/adr/generations/batch') {
        const generations = (body?.commits || []).map((sha, index) => ({
          id: `batch-gen-${index}`, status: 'queued', source_kind: 'commit', source_ref: sha,
        }))
        return { batch_id: 'batch-1', total: generations.length, generations }
      }
      if (path === '/api/adr/drafts/d1/review') {
        const status = body?.action === 'approve' ? 'accepted' : 'rejected'
        const reviewStatus = body?.action === 'approve' ? 'approved' : 'rejected'
        drafts[0] = {
          ...drafts[0], status, review_status: reviewStatus,
          rating: body?.rating ?? drafts[0].rating,
          review_comment: body?.comment || '',
          reviewed_at: '2026-11-25T01:00:00Z',
          markdown: '# 测试 ADR\n\nstatus: ' + status + '\n',
        }
        return { draft: drafts[0], backtest: BACKTEST }
      }
      if (path === '/api/adr/drafts/d1/backtest') {
        drafts[0] = { ...drafts[0], backtest: { verdict: 'partial', confidence: 0.66, status: 'ready' } }
        return { backtest: BACKTEST }
      }
      if (path === '/api/adr/drafts/d1') {
        const revision = (drafts[0].revision || 1) + 1
        const revisions = [
          {
            id: `rev-${revision}`, revision, markdown: body?.markdown || '',
            title: body?.title || drafts[0].title, editor: body?.editor || '',
            note: body?.note || '', created_at: '2026-11-25T02:00:00Z',
          },
          ...(drafts[0].revisions || []),
        ]
        drafts[0] = {
          ...drafts[0], revision, markdown: body?.markdown || drafts[0].markdown,
          title: body?.title || drafts[0].title, editor: body?.editor || '',
          status: 'proposed', review_status: 'pending', rating: null,
          review_comment: '', reviewed_at: null, reviewer: '', revisions,
        }
        return { draft: drafts[0], revisions, backtest: null }
      }
      if (path === '/api/adr/drafts/d1/export') {
        drafts[0] = { ...drafts[0], exported_path: 'docs/adr/0001-test-adr.md' }
        return { relative_path: 'docs/adr/0001-test-adr.md', path: '/repo/docs/adr/0001-test-adr.md' }
      }
      if (path === '/api/adr/template') {
        template = {
          ...template,
          template: {
            id: 't1', version: 1, template_text: body?.template_text || '',
            template_digest: 'sha256:custom',
          },
          template_digest: 'sha256:custom', template_version: 1,
        }
        return template
      }
      return {}
    }),
    delete: vi.fn(async () => ({ deleted: true })),
  }

  const wrapper = mount(Knowledge, {
    props: { repoName: 'shop' },
    global: {
      mocks: {
        $api: api,
        $route: { query: { snapshot_id: 'snapshot-1' } },
        $runAsync: async (task) => task(),
      },
    },
  })
  return { wrapper, api, posted, drafts: () => drafts }
}

async function openWorkbench() {
  const context = mountKnowledge()
  await flushPromises()
  context.wrapper.vm.activeSection = 'adr'
  await flushPromises()
  return context
}

describe('ADR workbench integration', () => {
  it('renders the commit table and batch generates selected candidates', async () => {
    const { wrapper, posted } = await openWorkbench()

    expect(wrapper.find('[data-testid="adr-commit-table"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('feat: add payment gateway')
    expect(wrapper.vm.adrCommitFilterCandidates).toBe(true)
    expect(wrapper.vm.adrCommitCandidateCount).toBe(3)

    // The default filter hides skipped commits; turn it off to inspect them.
    await wrapper.find('[data-testid="adr-commit-filter"]').setValue(false)
    await flushPromises()

    const skipped = wrapper.find('[data-testid="adr-commit-select-sha-docs"]')
    expect(skipped.exists()).toBe(true)
    expect(skipped.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('仅修改文档、测试或 lockfile')
    expect(wrapper.text()).toContain('待 AI 分诊')

    const candidate = wrapper.find('[data-testid="adr-commit-select-sha-feat"]')
    expect(candidate.attributes('disabled')).toBeUndefined()
    await candidate.setValue(true)
    await wrapper.find('[data-testid="adr-batch-generate"]').trigger('click')
    await flushPromises()

    const batch = posted.find(item => item.path === '/api/adr/generations/batch')
    expect(batch).toBeTruthy()
    expect(batch.body).toMatchObject({ snapshot_id: 'snapshot-1', commits: ['sha-feat'] })
    wrapper.unmount()
  })

  it('renders lifecycle progress and filters commits by stage server-side', async () => {
    const { wrapper, api } = await openWorkbench()

    // Per-commit stage is rendered; the table no longer shows a global
    // processed/total bar (progress belongs to the selected generation batch).
    expect(wrapper.find('[data-testid="adr-commit-progress"]').exists()).toBe(false)
    expect(wrapper.vm.adrCommits.every(item => typeof item.stage === 'string')).toBe(true)
    expect(wrapper.vm.adrCommitStageText({ stage: 'generating', generation_progress: 40 })).toBe('生成中 40%')
    expect(wrapper.find('[data-testid="adr-commit-stage-sha-generated"]').text()).toContain('待校验')

    // The 待校验 chip asks the server for that stage only.
    await wrapper.find('[data-testid="adr-stage-filter-generated"]').trigger('click')
    await flushPromises()

    const commitCalls = api.get.mock.calls.filter(([path]) => path === '/api/adr/commits')
    expect(commitCalls.at(-1)[1]).toMatchObject({ snapshot_id: 'snapshot-1', stage: 'generated' })
    expect(wrapper.vm.adrStageFilter).toBe('generated')
    expect(wrapper.find('[data-testid="adr-commit-row-sha-generated"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="adr-commit-stage-sha-generated"]').text()).toContain('待校验')
    expect(wrapper.find('[data-testid="adr-commit-row-sha-feat"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="adr-commit-row-sha-docs"]').exists()).toBe(false)

  it('shows how many selected commits finished executing', async () => {
    const { wrapper } = await openWorkbench()
    wrapper.vm.adrBatchId = 'batch-1'
    wrapper.vm.adrBatch = {
      batch_id: 'batch-1',
      counts: { queued: 0, running: 1, succeeded: 2, skipped: 0, failed: 0 },
      progress: {
        total: 3, completed: 2, failed: 0, skipped: 0,
        running: 1, queued: 0, percent: 67,
      },
    }
    await flushPromises()

    const progress = wrapper.find('[data-testid="adr-batch-progress"]')
    expect(progress.exists()).toBe(true)
    expect(progress.text()).toContain('批量生成进度（已选 3 个提交）')
    expect(progress.text()).toContain('已完成 2/3（67%）')
    expect(wrapper.vm.adrBatchProgress).toMatchObject({ total: 3, completed: 2, percent: 67 })
    wrapper.unmount()
  })

    // Selecting 全部 clears the filter and requests the unfiltered list again.
    await wrapper.find('[data-testid="adr-stage-filter-all"]').trigger('click')
    await flushPromises()
    expect(wrapper.vm.adrStageFilter).toBe('')
    expect(wrapper.find('[data-testid="adr-commit-row-sha-feat"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('keeps row actions aligned with the commit stage', async () => {
    const { wrapper } = await openWorkbench()

    // pending → 生成 enabled, later stages disabled.
    expect(wrapper.find('[data-testid="adr-commit-generate-sha-feat"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="adr-commit-backtest-sha-feat"]').attributes('disabled')).toBeDefined()

    // generated → 回测 enabled, 生成 disabled.
    expect(wrapper.find('[data-testid="adr-commit-generate-sha-generated"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="adr-commit-backtest-sha-generated"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="adr-commit-review-sha-generated"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="adr-commit-export-sha-generated"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('approves a draft and enables export', async () => {
    const { wrapper, posted } = await openWorkbench()

    expect(wrapper.find('[data-testid="adr-draft-export-d1"]').attributes('disabled')).toBeDefined()

    await wrapper.vm.openAdrDraft({ id: 'd1' })
    await flushPromises()
    expect(wrapper.find('[data-testid="adr-detail"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="adr-detail-backtest"]').exists()).toBe(true)

    await wrapper.find('[data-testid="adr-review-approve"]').trigger('click')
    await flushPromises()

    const review = posted.find(item => item.path === '/api/adr/drafts/d1/review')
    expect(review).toBeTruthy()
    expect(review.body).toMatchObject({ action: 'approve' })
    expect(wrapper.text()).toContain('已通过')

    const exportButton = wrapper.find('[data-testid="adr-draft-export-d1"]')
    expect(exportButton.exists()).toBe(true)
    expect(exportButton.attributes('disabled')).toBeUndefined()

    await exportButton.trigger('click')
    await flushPromises()
    expect(posted.some(item => item.path === '/api/adr/drafts/d1/export')).toBe(true)
    expect(wrapper.text()).toContain('docs/adr/0001-test-adr.md')
    wrapper.unmount()
  })

  it('saves an edited draft as a new revision and resets review', async () => {
    const { wrapper, posted } = await openWorkbench()

    await wrapper.vm.openAdrDraft({ id: 'd1' })
    await flushPromises()
    await wrapper.find('[data-testid="adr-detail-edit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="adr-editor"]').exists()).toBe(true)
    await wrapper.find('[data-testid="adr-edit-markdown"]').setValue('# 测试 ADR\n\n## Context\n\n精修后的内容\n')
    await wrapper.find('[data-testid="adr-edit-title"]').setValue('测试 ADR（精修）')
    await wrapper.find('[data-testid="adr-edit-editor"]').setValue('bob')
    await wrapper.find('[data-testid="adr-edit-note"]').setValue('精简')
    await wrapper.find('[data-testid="adr-edit-save"]').trigger('click')
    await flushPromises()

    const saved = posted.find(item => item.path === '/api/adr/drafts/d1')
    expect(saved).toBeTruthy()
    expect(saved.body).toMatchObject({
      markdown: '# 测试 ADR\n\n## Context\n\n精修后的内容\n',
      title: '测试 ADR（精修）',
      editor: 'bob',
      note: '精简',
    })
    expect(wrapper.text()).toContain('已保存为版本 v2')
    expect(wrapper.find('[data-testid="adr-editor"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="adr-revisions"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('版本历史')

    await wrapper.vm.exportAdrDraft({ id: 'd1' })
    await flushPromises()
    expect(wrapper.find('[data-testid="adr-draft-edit-d1"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="adr-review-reviewer"]').setValue('alice')
    await wrapper.find('[data-testid="adr-review-approve"]').trigger('click')
    await flushPromises()
    const review = posted.find(item => item.path === '/api/adr/drafts/d1/review')
    expect(review.body).toMatchObject({ action: 'approve', reviewer: 'alice' })
    wrapper.unmount()
  })

  it('saves the MADR output template for the service', async () => {
    const { wrapper, posted } = await openWorkbench()

    expect(wrapper.find('[data-testid="adr-template-settings"]').exists()).toBe(true)
    const textarea = wrapper.find('[data-testid="adr-template-textarea"]')
    expect(textarea.element.value).toContain('{{title}}')
    expect(wrapper.text()).toContain('{{options_detail}}')

    const custom = '# {{title}}\n\n## Context\n\n{{context}}\n\n## Decision\n\n{{outcome}}\n'
    await textarea.setValue(custom)
    await wrapper.find('[data-testid="adr-template-save"]').trigger('click')
    await flushPromises()

    const saved = posted.find(item => item.path === '/api/adr/template')
    expect(saved).toBeTruthy()
    expect(saved.body).toMatchObject({ snapshot_id: 'snapshot-1', template_text: custom })
    expect(wrapper.text()).toContain('已保存输出模板')
    wrapper.unmount()
  })

  it('generates from text and shows the debug mapping', async () => {
    const { wrapper, posted } = await openWorkbench()

    await wrapper.find('[data-testid="adr-tab-text"]').trigger('click')
    await wrapper.find('[data-testid="adr-text-input"]').setValue('把 ADR 生成放到后台线程池')
    await flushPromises()

    await wrapper.find('[data-testid="adr-generate-submit"]').trigger('click')
    await flushPromises()

    const generation = posted.find(item => item.path === '/api/adr/generations')
    expect(generation).toBeTruthy()
    expect(generation.body).toMatchObject({
      snapshot_id: 'snapshot-1',
      source: { kind: 'text', text: '把 ADR 生成放到后台线程池' },
    })

    // The API mock reports the generation as running; point it at a finished one and refresh.
    wrapper.vm.adrGeneration = { id: 'g2', status: 'running' }
    await wrapper.vm.refreshAdrGeneration()
    await flushPromises()
    expect(wrapper.text()).toContain('测试 ADR')

    await wrapper.vm.openAdrDraft({ id: 'd1' })
    await flushPromises()
    expect(wrapper.find('[data-testid="adr-detail"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('调试映射')
    expect(wrapper.html()).toContain('text {{text}}')
    wrapper.unmount()
  })

  it('exposes five prompt kinds and the template panel', async () => {
    const { wrapper } = await openWorkbench()

    expect(wrapper.find('[data-testid="adr-prompt-settings"]').exists()).toBe(true)
    expect(wrapper.vm.adrPromptTabs.map(tab => tab.key)).toEqual(['commit', 'file', 'text', 'triage', 'backtest'])
    expect(wrapper.vm.adrPromptText).toContain('{{diff}}')
    expect(wrapper.find('[data-testid="adr-template-textarea"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
