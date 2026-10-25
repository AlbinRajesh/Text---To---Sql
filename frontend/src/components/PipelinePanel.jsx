import { useEffect, useRef, useState } from 'react'
import Icon from './icon.jsx'
import { ESCALATION_STEP, STEPS, doneCount, progressPercent, totalSeconds } from '../utils/pipeline.js'
import { copyText } from '../utils/csv.js'

function useNow(active) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return undefined
    const t = setInterval(() => setNow(Date.now()), 200)
    return () => clearInterval(t)
  }, [active])
  return now
}

function StepRow({ step, data, now }) {
  const { state, seconds, startedAt } = data
  const slow = state === 'running' && startedAt && now - startedAt > 10_000
  const isEscalation = step.id === ESCALATION_STEP.id

  let circle
  if (state === 'done') {
    circle = isEscalation
      ? 'border-warn/40 bg-warn/20 text-warn'
      : 'border-ok/40 bg-ok/20 text-ok'
  } else if (state === 'failed') circle = 'border-bad/40 bg-bad/20 text-bad'
  else if (state === 'running') circle = 'border-primary bg-primary/20 text-primary'
  else circle = 'border-line bg-surface text-muted'

  const icon = state === 'done' ? (isEscalation ? 'support_agent' : 'check') : state === 'failed' ? 'close' : null

  return (
    <li
      className={`relative flex items-start gap-3.5 ${state === 'waiting' || state === 'skipped' ? 'opacity-50' : ''} ${
        state === 'running' ? '-mx-1.5 rounded-xl border border-primary/40 bg-surface p-2.5 shadow-md' : ''
      }`}
    >
      <div className={`z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border ${circle} ${state === 'running' ? '' : 'bg-bg'}`}>
        {state === 'running' ? <Icon name="sync" size={14} spin /> : icon ? <Icon name={icon} size={14} /> : <span className="h-1.5 w-1.5 rounded-full bg-muted" />}
      </div>
      <div className="min-w-0 flex-1 pt-0.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className={`text-xs ${state === 'running' ? 'font-bold' : 'font-semibold'} text-ink`}>{step.label}</span>
          {state === 'done' && seconds !== null && <span className="font-mono text-[11px] text-ok">{seconds}s</span>}
          {state === 'skipped' && <span className="text-[11px] text-muted">Skipped</span>}
          {state === 'failed' && <span className="text-[11px] text-bad">Failed</span>}
        </div>
        {slow && <p className="mt-1 text-[11px] text-muted">Still working… this step can take a while.</p>}
      </div>
    </li>
  )
}

export default function PipelinePanel({ pipeline, generatedSql, open, onClose }) {
  const running = Boolean(pipeline && !pipeline.finishedAt)
  const now = useNow(running)
  const [expandedFor, setExpandedFor] = useState(null)
  const expanded = expandedFor === pipeline?.startedAt
  const [copyState, setCopyState] = useState({ sql: null, status: 'idle' })
  const copyStatus = copyState.sql === generatedSql ? copyState.status : 'idle'
  const copyTimer = useRef(null)

  useEffect(() => () => clearTimeout(copyTimer.current), [])
  useEffect(() => {
    clearTimeout(copyTimer.current)
  }, [generatedSql])

  const copySql = async () => {
    const copied = await copyText(generatedSql)
    const status = copied ? 'copied' : 'failed'
    setCopyState({ sql: generatedSql, status })
    clearTimeout(copyTimer.current)
    copyTimer.current = setTimeout(() => {
      setCopyState((current) => (
        current.sql === generatedSql
          ? { sql: generatedSql, status: 'idle' }
          : current
      ))
    }, 2000)
  }

  const finished = Boolean(pipeline?.finishedAt)
  const percent = progressPercent(pipeline)
  const elapsed = pipeline ? ((pipeline.finishedAt || now) - pipeline.startedAt) / 1000 : 0
  const showEscalation = pipeline && pipeline.steps.escalation.state !== 'waiting'

  const outcomeText = {
    chat: `Replied in ${(totalSeconds(pipeline) ?? 0).toFixed(1)}s`,
    ok: `Answered in ${(totalSeconds(pipeline) ?? 0).toFixed(1)}s, ${doneCount(pipeline)} steps`,
    unanswerable: `Finished in ${(totalSeconds(pipeline) ?? 0).toFixed(1)}s`,
    escalated: 'Sent for human review',
    failed: 'Request failed',
    stopped: 'Stopped',
  }[pipeline?.outcome]

  return (
    <aside
      className={`fixed inset-y-0 right-0 z-50 flex w-[380px] max-w-[92vw] flex-col border-l border-line bg-bg transition-transform duration-200 xl:static xl:z-auto xl:w-[400px] xl:translate-x-0 ${
        open ? 'translate-x-0' : 'translate-x-full'
      }`}
      aria-label="How your answer is made"
    >
      <div className="flex flex-col gap-3 border-b border-line bg-surface/60 p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className={`h-2.5 w-2.5 rounded-full bg-accent ${running ? 'animate-pulse' : ''}`} />
            <h2 className="text-sm font-bold text-ink">How your answer is made</h2>
          </div>
          <div className="flex items-center gap-2">
            {pipeline && <span className="rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 font-mono text-xs text-accent">{elapsed.toFixed(1)}s</span>}
            <button type="button" onClick={onClose} aria-label="Close panel" className="rounded-md p-1 text-muted hover:bg-surface-high hover:text-ink xl:hidden">
              <Icon name="close" size={18} />
            </button>
          </div>
        </div>

        {pipeline && (
          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between text-[11px] text-muted">
              <span>Progress</span>
              <span className="font-mono text-accent">{percent}%</span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-high" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
              <div className="h-full rounded-full bg-gradient-to-r from-primary to-accent transition-all duration-500" style={{ width: `${percent}%` }} />
            </div>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-5">
        {!pipeline && <p className="text-sm text-muted">Ask a question and each step will show up here.</p>}

        {pipeline && finished && (
          <button
            type="button"
            onClick={() => setExpandedFor(expanded ? null : pipeline.startedAt)}
            aria-expanded={expanded}
            className="mb-4 flex w-full items-center justify-between rounded-xl border border-line bg-surface p-3 text-left text-xs font-medium text-ink hover:bg-surface-high"
          >
            <span className="flex items-center gap-2">
              <Icon
                name={['ok', 'unanswerable', 'chat'].includes(pipeline.outcome) ? 'check_circle' : pipeline.outcome === 'escalated' ? 'support_agent' : 'error'}
                size={16}
                className={pipeline.outcome === 'failed' ? 'text-bad' : pipeline.outcome === 'escalated' ? 'text-warn' : 'text-ok'}
              />
              {outcomeText}
            </span>
            <Icon name="expand_more" size={18} className={`text-muted transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </button>
        )}

        {pipeline && (!finished || expanded) && (
          <div className="relative">
            <div className="pointer-events-none absolute bottom-3 left-3 top-3 w-px bg-line" />
            <ol className="relative space-y-3">
              {STEPS.map((s) => (
                <StepRow key={s.id} step={s} data={pipeline.steps[s.id]} now={now} />
              ))}
              {showEscalation && <StepRow step={ESCALATION_STEP} data={pipeline.steps.escalation} now={now} />}
            </ol>
          </div>
        )}

        {typeof generatedSql === 'string' && generatedSql.length > 0 && (
          <section className="mt-6 border-t border-line pt-4" aria-label="Generated SQL">
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="text-xs font-bold text-ink">Generated SQL</h3>
              <button
                type="button"
                onClick={copySql}
                className={`flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-medium transition-colors ${
                  copyStatus === 'copied'
                    ? 'border-ok/40 bg-ok/10 text-ok'
                    : copyStatus === 'failed'
                      ? 'border-bad/40 bg-bad/10 text-bad'
                      : 'border-line bg-surface text-muted hover:bg-surface-high hover:text-ink'
                }`}
                aria-label={copyStatus === 'copied' ? 'SQL copied' : 'Copy SQL'}
              >
                <Icon name={copyStatus === 'copied' ? 'check' : 'content_copy'} size={14} />
                {copyStatus === 'copied' ? 'Copied!' : copyStatus === 'failed' ? 'Copy failed' : 'Copy SQL'}
              </button>
            </div>
            <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-200">
              <code>{generatedSql}</code>
            </pre>
          </section>
        )}
      </div>
    </aside>
  )
}
