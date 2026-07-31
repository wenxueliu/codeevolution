export class HttpError extends Error {
  constructor(message, { status = 0, url = '', body = null, cause = null } = {}) {
    super(message, { cause })
    this.name = 'HttpError'
    this.status = status
    this.url = url
    this.body = body
  }
}

export function encodeQuery(values = {}) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  }
  return query.toString()
}

export function createApiClient(fetchAdapter = globalThis.fetch) {
  if (typeof fetchAdapter !== 'function') throw new TypeError('A fetch adapter is required')

  async function request(path, { query, ...options } = {}) {
    const suffix = encodeQuery(query)
    const url = suffix ? `${path}${path.includes('?') ? '&' : '?'}${suffix}` : path
    let response
    try {
      response = await fetchAdapter(url, options)
    } catch (cause) {
      throw new HttpError(`Network request failed: ${url}`, { url, cause })
    }
    const contentType = response.headers?.get?.('content-type') || ''
    const body = contentType.includes('application/json') ? await response.json() : await response.text()
    if (!response.ok) {
      const detail = body?.detail || body?.message || response.statusText || 'Request failed'
      throw new HttpError(detail, { status: response.status, url, body })
    }
    return body
  }

  return {
    get(path, query) { return request(path, { query }) },
    request,
  }
}

export const apiClient = createApiClient()
