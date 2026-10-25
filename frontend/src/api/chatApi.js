import { STEPS } from '../utils/pipeline.js'

const BASE = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '')
const MOCK = import.meta.env.VITE_MOCK === 'true'

/** kind: 'validation' | 'server' | 'network' | 'timeout' */
export class ApiError extends Error {
  constructor(kind, message, extra = {}) {
    super(message)
    this.kind = kind
    Object.assign(this, extra)
  }
}

const NETWORK_MSG = "Can't reach the server. Check your connection and try again."
const SERVER_MSG = 'Something went wrong on our side. Please try again.'

async function safeJson(res) {
  try {
    return await res.json()
  } catch {
    return null
  }
}

function validationError(body) {
  const msg = Array.isArray(body?.detail)
    ? body.detail.map((d) => d.msg).filter(Boolean).join(' ')
    : 'Please check your question and try again.'
  return new ApiError('validation', msg)
}

/** GET /api/health -> true / false */
export async function checkHealth(signal) {
  if (MOCK) return true
  try {
    const res = await fetch(`${BASE}/api/health`, { signal })
    return res.ok
  } catch {
    return false
  }
}

/** Plain POST /api/chat (final answer only). Returns the result body. */
async function postChat(question, signal, conversationId) {
  let res
  try {
    res = await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, conversation_id: conversationId }),
      signal,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError('network', NETWORK_MSG)
  }
  if (res.ok) return res.json()
  const body = await safeJson(res)
  if (res.status === 422) throw validationError(body)
  // 500 / 504 bodies still carry request_id + status "escalated" + message
  if (body?.status) return body
  throw new ApiError('server', SERVER_MSG)
}

/**
 * Fallback: use /api/chat and animate the stepper from a timer.
 * Used only when /api/chat/stream is missing (404/405) or unreachable before any event.
 */
async function fallbackChat(question, conversationId, signal, onEvent) {
  let i = 0
  let current = null
  const tick = () => {
    if (current) onEvent({ type: 'node_done', node: current })
    if (i < STEPS.length - 1) {
      current = STEPS[i++].id
      onEvent({ type: 'node_start', node: current })
    }
  }
  tick()
  const timer = setInterval(tick, 1200)
  try {
    const body = await postChat(question, signal, conversationId)
    onEvent({ type: 'result', ...body, fallback: true })
  } finally {
    clearInterval(timer)
  }
}

/**
 * Main entry. Calls onEvent for every event, ending with { type: 'result', ...body }.
 * Throws ApiError (or AbortError when cancelled).
 */
export async function streamChat({
  question,
  conversationId,
  suggestionId,
  signal,
  onEvent,
}) {
  if (suggestionId) {
    let res
    try {
      res = await fetch(`${BASE}/api/chat/suggestion`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId,
          suggestion_id: suggestionId,
        }),
        signal,
      })
    } catch (e) {
      if (e.name === 'AbortError') throw e
      throw new ApiError('network', NETWORK_MSG)
    }
    const body = await safeJson(res)
    if (res.ok) {
      onEvent({ type: 'result', ...body })
      return
    }
    if (![404, 405, 410, 503].includes(res.status)) {
      throw new ApiError('server', body?.message || SERVER_MSG)
    }
    console.info(
      `Suggestion fast path unavailable (HTTP ${res.status}); submitting the corrected question normally.`,
    )
  }

  if (MOCK) return mockStream(question, signal, onEvent)

  let res
  try {
    res = await fetch(`${BASE}/api/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, conversation_id: conversationId }),
      signal,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    // Network failure before the first event: safe to try the plain endpoint.
    return fallbackChat(question, conversationId, signal, onEvent)
  }

  if (res.status === 404 || res.status === 405) {
    return fallbackChat(question, conversationId, signal, onEvent)
  }

  if (!res.ok || !res.body) {
    const body = await safeJson(res)
    if (res.status === 422) throw validationError(body)
    if (body?.status) {
      onEvent({ type: 'result', ...body })
      return
    }
    throw new ApiError('server', SERVER_MSG)
  }

  // NDJSON: one JSON object per line.
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let gotResult = false

  const handleLine = (line) => {
    let ev
    try {
      ev = JSON.parse(line)
    } catch {
      return
    }
    if (ev.type === 'result') gotResult = true
    onEvent(ev)
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).trim()
        buffer = buffer.slice(idx + 1)
        if (line) handleLine(line)
      }
    }
    if (buffer.trim()) handleLine(buffer.trim())
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError('network', 'The connection was interrupted. Please try again.')
  }

  if (!gotResult) throw new ApiError('server', 'The connection ended before an answer arrived. Please try again.')
}

/* ------------------------------ MOCK MODE ------------------------------ */

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'))
    const t = setTimeout(resolve, ms)
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(t)
        reject(new DOMException('Aborted', 'AbortError'))
      },
      { once: true },
    )
  })
}

function mockResult(question) {
  const q = question.toLowerCase()
  const base = { request_id: crypto.randomUUID(), message: null }
  if (q.includes('bitcoin') || q.includes('meaning of life')) {
    return { ...base, status: 'unanswerable', results: null, columns: null, count: null, message: "I can't answer that from this database." }
  }
  if (q.includes('escalate')) {
    return { ...base, status: 'escalated', results: null, columns: null, count: null, message: 'This question needs a human to review it.' }
  }
  if (q.includes('how many') || q.includes('total')) {
    return { ...base, status: 'ok', results: [{ total_plans: 24 }], columns: ['total_plans'], count: 1 }
  }
  if (q.includes('average')) {
    return { ...base, status: 'ok', results: [{ average_price: 348.5 }], columns: ['average_price'], count: 1 }
  }
  const results = [
    { plan_name: 'Enterprise Growth', tier: 'Tier 4', monthly_price: 1250 },
    { plan_name: 'Global Cloud Scale', tier: 'Tier 4', monthly_price: 890 },
    { plan_name: 'Dedicated Cluster', tier: 'Tier 3', monthly_price: 650 },
    { plan_name: 'Compliance Suite Pro', tier: 'Tier 3', monthly_price: 520 },
  ]
  return { ...base, status: 'ok', results, columns: ['plan_name', 'tier', 'monthly_price'], count: results.length }
}

async function mockStream(question, signal, onEvent) {
  const key = question.trim().toLowerCase()
  const timings = { classify: 0.7, grade_schema: 1.3, business_rules: 0.4, generate_query: 1.8, safety_check: 0.3, execute: 0.4, result_grader: 1.0, return_save: 0.3 }
  const escalate = key.includes('escalate')

  for (const step of STEPS) {
    const secs = timings[step.id]
    if (secs === undefined) {
      onEvent({ type: 'node_skipped', node: step.id })
      continue
    }
    onEvent({ type: 'node_start', node: step.id })
    await sleep(secs * 1000, signal)
    onEvent({ type: 'node_done', node: step.id, seconds: secs })
    if (escalate && step.id === 'result_grader') break
  }
  if (escalate) {
    onEvent({ type: 'node_start', node: 'escalation' })
    await sleep(400, signal)
    onEvent({ type: 'node_done', node: 'escalation', seconds: 0.4 })
  }
  const result = mockResult(question)
  onEvent({ type: 'result', ...result })
}
