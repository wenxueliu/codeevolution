import assert from 'node:assert/strict'
import test from 'node:test'

import { HttpError, createApiClient, encodeQuery } from '../src/api/apiClient.js'

test('encodeQuery omits empty values and encodes repository names', () => {
  assert.equal(encodeQuery({ repo: 'order service', search: '', limit: 10 }), 'repo=order+service&limit=10')
})

test('api client uses the injected fetch adapter', async () => {
  const calls = []
  const client = createApiClient(async (url) => {
    calls.push(url)
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ ok: true }) }
  })
  assert.deepEqual(await client.get('/api/features', { repo: 'orders' }), { ok: true })
  assert.deepEqual(calls, ['/api/features?repo=orders'])
})

test('api client sends DELETE requests', async () => {
  const calls = []
  const client = createApiClient(async (url, options) => {
    calls.push([url, options.method])
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ ok: true }) }
  })
  assert.deepEqual(await client.delete('/api/repos/orders'), { ok: true })
  assert.deepEqual(calls, [['/api/repos/orders', 'DELETE']])
})

test('api client exposes a consistent HTTP error', async () => {
  const client = createApiClient(async () => ({
    ok: false, status: 404, statusText: 'Not Found',
    headers: { get: () => 'application/json' }, json: async () => ({ detail: 'missing' }),
  }))
  await assert.rejects(client.get('/missing'), (error) => {
    assert.ok(error instanceof HttpError)
    assert.equal(error.status, 404)
    assert.equal(error.message, 'missing')
    return true
  })
})
