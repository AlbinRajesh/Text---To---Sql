// Steps shown in the pipeline panel. `id` must match the node names the backend streams.
export const STEPS = [
  { id: 'classify', label: 'Understanding your question' },
  { id: 'cache_check', label: 'Checking saved answers' },
  { id: 'grade_schema', label: 'Finding the right data' },
  { id: 'business_rules', label: 'Applying business rules' },
  { id: 'generate_query', label: 'Writing the query' },
  { id: 'safety_check', label: 'Safety check' },
  { id: 'execute', label: 'Running on the database' },
  { id: 'result_grader', label: 'Verifying the answer' },
  { id: 'return_save', label: 'Preparing your result' },
]

export const ESCALATION_STEP = { id: 'escalation', label: 'Needs human review' }

const ALL_IDS = [...STEPS.map((s) => s.id), ESCALATION_STEP.id]

/** Fresh pipeline: every step "waiting". */
export function newPipeline() {
  const steps = {}
  ALL_IDS.forEach((id) => {
    steps[id] = { state: 'waiting', seconds: null, startedAt: null }
  })
  return { steps, startedAt: Date.now(), finishedAt: null, outcome: null }
}

/** Apply one streamed event (node_start / node_done / node_skipped). Unknown nodes are ignored. */
export function applyEvent(pipeline, ev) {
  if (!pipeline || !ev?.node || !pipeline.steps[ev.node]) return pipeline
  const cur = pipeline.steps[ev.node]
  let next
  if (ev.type === 'node_start') next = { ...cur, state: 'running', startedAt: Date.now() }
  else if (ev.type === 'node_done') next = { ...cur, state: 'done', seconds: ev.seconds ?? null }
  else if (ev.type === 'node_skipped') next = { ...cur, state: 'skipped' }
  else return pipeline
  return { ...pipeline, steps: { ...pipeline.steps, [ev.node]: next } }
}

/**
 * Close the pipeline when the request ends.
 * outcome: 'ok' | 'unanswerable' | 'escalated' | 'failed' | 'stopped'
 * fallback: true when we only had the plain /api/chat answer (no real events).
 */
export function finalizePipeline(pipeline, { outcome, fallback = false }) {
  if (!pipeline) return pipeline
  const steps = {}
  for (const [id, s] of Object.entries(pipeline.steps)) {
    let state = s.state
    if (state === 'running') {
      state = outcome === 'failed' ? 'failed' : outcome === 'stopped' ? 'skipped' : 'done'
    } else if (state === 'waiting' && id !== ESCALATION_STEP.id) {
            state = fallback && ['ok', 'unanswerable', 'chat'].includes(outcome) ? 'done' : 'skipped'
    }
    steps[id] = { ...s, state }
  }
  if (outcome === 'escalated' && steps.escalation.state === 'waiting') {
    steps.escalation = { ...steps.escalation, state: 'done' }
  }
  return { ...pipeline, steps, finishedAt: Date.now(), outcome }
}

export function progressPercent(pipeline) {
  if (!pipeline) return 0
  if (pipeline.finishedAt) return 100
  const finished = STEPS.filter((s) => ['done', 'skipped', 'failed'].includes(pipeline.steps[s.id].state)).length
  return Math.round((finished / STEPS.length) * 100)
}

export function doneCount(pipeline) {
  if (!pipeline) return 0
  return STEPS.filter((s) => pipeline.steps[s.id].state === 'done').length
}

export function totalSeconds(pipeline) {
  if (!pipeline?.finishedAt) return null
  return (pipeline.finishedAt - pipeline.startedAt) / 1000
}
