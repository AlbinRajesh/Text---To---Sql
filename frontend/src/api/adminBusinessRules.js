const BASE = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '')
const ROOT = `${BASE}/api/admin/business-rules`

async function responseError(response) {
  let detail = `Request failed with HTTP ${response.status}.`
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') detail = body.detail
    else if (Array.isArray(body?.detail)) {
      detail = body.detail.map((item) => item.msg).filter(Boolean).join(' ')
    }
  } catch {
    // Keep the HTTP status message when the server does not return JSON.
  }
  const error = new Error(detail)
  error.status = response.status
  return error
}

async function request(path, key, options = {}) {
  let response
  try {
    response = await fetch(`${ROOT}${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${key}`,
        ...options.headers,
      },
    })
  } catch (error) {
    throw new Error(`Could not reach the admin API: ${error.message}`, { cause: error })
  }
  if (!response.ok) throw await responseError(response)
  return response
}

export async function exportBusinessRules(key) {
  const response = await request('/current.md', key)
  return response.text()
}

export async function createBusinessRuleDraft(key, markdown) {
  const response = await request('/drafts', key, {
    method: 'POST',
    headers: { 'Content-Type': 'text/markdown' },
    body: markdown,
  })
  return response.json()
}

export async function getBusinessRuleDraft(key, revisionId) {
  const response = await request(`/drafts/${encodeURIComponent(revisionId)}`, key)
  return response.json()
}

export async function publishBusinessRuleDraft(key, revisionId) {
  const response = await request(
    `/drafts/${encodeURIComponent(revisionId)}/publish`,
    key,
    { method: 'POST' },
  )
  return response.json()
}

export async function listBusinessRuleRevisions(key) {
  const response = await request('/revisions?limit=50', key)
  const body = await response.json()
  return body.revisions
}
