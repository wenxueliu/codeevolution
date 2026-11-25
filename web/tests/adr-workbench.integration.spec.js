import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import Knowledge from '../src/pages/Knowledge.vue'

function mountKnowledge() {
  const posted = []
  let drafts = []
  const api = {
    get: vi.fn(async (path) => {
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
          ],
        }
      }
      if (path === '/api/adr/drafts') return { drafts }
      if (path === '/api/adr/generations/g1') {
        return {
          generation: {
            id: 'g1', status: 'succeeded', stage: 'draft_ready', progress: 100,
            source_kind: 'text', source_ref: 'abc', source_payload: { text: { text: '决定' } },
            source_digest: 'sha256:s', prompt_id: '', prompt_version: 0, prompt_digest: 'sha256:p',
            prompt_text: 'text {{text}}', prompt_rendered: 'text 决定', model: 'fake-model',
            raw_response: '{"is_decision": true}', parsed: { is_decision: true }, error_message: '',
            skip_reason: '', draft_id: 'd1', duration_ms: 12, created_at: '2026-11-25T00:00:00Z',
          },
          draft: {
            id: 'd1', title: '测试 ADR', slug: 'test-adr', status: 'proposed',
            markdown: '# 测试 ADR\n', exported_path: '', created_at: '2026-11-25T00:00:00Z',
          },
        }
      }
      if (path === '/api/adr/drafts/d1') {
        return {
          draft: { id: 'd1', title: '测试 ADR', slug: 'test-adr', status: 'proposed', markdown: '# 测试 ADR\n' },
          generation: {
            id: 'g1', status: 'succeeded', source_kind: 'text', source_ref: 'abc',
            source_payload: { text: { text: '决定' } }, source_digest: 'sha256:s',
            prompt_id: '', prompt_version: 0, prompt_digest: 'sha256:p', prompt_text: 'text {{text}}',
            model: 'fake-model', raw_response: '{}', parsed: { is_decision: true },
            error_message: '', skip_reason: '', duration_ms: 12,
          },
        }
      }
      return { rules: [] }
    }),
    request: vi.fn(async (path, options = {}) => {
      posted.push({ path, body: options.body ? JSON.parse(options.body) : null })
      if (path === '/api/adr/generations') {
        return { generation: { id: 'g1', status: 'running', stage: 'calling_model', progress: 10, source_kind: 'text' } }
      }
      if (path === '/api/adr/drafts/d1/export') {
        return { relative_path: 'docs/adr/0001-test-adr.md', path: '/repo/docs/adr/0001-test-adr.md' }
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
  return { wrapper, api, posted, setDrafts: (items) => { drafts = items } }
}

describe('ADR workbench integration', () => {
  it('generates from text and shows the debug mapping', async () => {
    const { wrapper, posted } = mountKnowledge()
    await flushPromises()
    wrapper.vm.activeSection = 'adr'
    await flushPromises()

    await wrapper.find('[data-testid="adr-tab-text"]').trigger('click')
    await wrapper.find('[data-testid="adr-text-input"]').setValue('把 ADR 生成放到后台线程池')
    await flushPromises()

    await wrapper.find('[data-testid="adr-generate-submit"]').trigger('click')
    await flushPromises()

    expect(posted[0].path).toBe('/api/adr/generations')
    expect(posted[0].body).toMatchObject({
      snapshot_id: 'snapshot-1',
      source: { kind: 'text', text: '把 ADR 生成放到后台线程池' },
    })

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

  it('renders prompt settings and exports a draft', async () => {
    const { wrapper, posted } = mountKnowledge()
    await flushPromises()
    wrapper.vm.activeSection = 'adr'
    await flushPromises()
    await wrapper.vm.loadAdrDrafts()
    await flushPromises()

    expect(wrapper.find('[data-testid="adr-prompt-settings"]').exists()).toBe(true)
    expect(wrapper.vm.adrPromptText).toContain('{{diff}}')

    await wrapper.vm.exportAdrDraft({ id: 'd1', title: '测试 ADR' })
    await flushPromises()
    expect(posted.some(item => item.path === '/api/adr/drafts/d1/export')).toBe(true)
    expect(wrapper.text()).toContain('docs/adr/0001-test-adr.md')
    wrapper.unmount()
  })
})
