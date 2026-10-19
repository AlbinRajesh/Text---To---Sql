import { useState } from 'react'
import Icon from './icon.jsx'
import QueryResultTable from './QueryResultTable.jsx'
import {
  columnLabel,
  copyText,
  formatCell,
  getColumns,
  isIdentifierColumn,
  toCSV,
} from '../utils/csv.js'
import { totalSeconds } from '../utils/pipeline.js'
import { answerSummary, followUpQuestions } from '../utils/answerPresentation.js'

const time = (ts) => new Date(ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })

function answerText(m) {
  const r = m.response
  if (m.status === 'ok' && r?.results?.length) {
    const cols = getColumns(r)
    const summary = answerSummary(m.question, r)
    if (r.answer_type === 'single_value' || (r.results.length === 1 && cols.length === 1)) {
      const column = cols[0]
      const value = r.single_value ?? r.results[0][column]
      const format = r.column_formats?.[column]
      if (format === 'identifier' || isIdentifierColumn(column)) {
        return `${columnLabel(column, r.column_labels)}: ${formatCell(value, 'identifier', column)}`
      }
      return summary || `${columnLabel(column, r.column_labels)}: ${formatCell(value, format, column)}`
    }
    return [summary, toCSV(cols, r.results, r.column_labels)].filter(Boolean).join('\n\n')
  }
  return r?.message || m.error?.message || ''
}

function ActionButton({ icon, label, onClick, active = false, disabled = false }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={`rounded-lg p-1.5 transition-colors hover:bg-surface-high disabled:opacity-40 ${active ? 'text-primary' : 'text-muted hover:text-ink'}`}
    >
      <Icon name={icon} size={16} />
    </button>
  )
}

function UserMessage({ message }) {
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col items-end gap-1.5">
      <div className="flex items-center gap-1.5 text-xs text-muted">
        <span className="font-medium text-ink">You</span>
        <span>{time(message.timestamp)}</span>
      </div>
      <div className="max-w-xl whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm border border-line bg-surface-high px-4 py-2.5 text-sm text-ink shadow-sm">
        {message.text}
      </div>
    </div>
  )
}

function AssistantMessage({ message, onRetry, onFeedback, onUseSuggestion, onAskFollowUp, notify, canRetry, isCurrent }) {
  const [followUpsDismissed, setFollowUpsDismissed] = useState(false)
  const { status, response, error, pipeline } = message
  const secs = totalSeconds(pipeline)
  const warn = status === 'escalated'
  const bad = status === 'error'
  const cacheHit = Boolean(response?.cache_hit)
  const summary = status === 'ok' ? answerSummary(message.question, response) : null
  const followUps = status === 'ok' && !followUpsDismissed
    ? followUpQuestions(message.question, response)
    : []

  const copyAnswer = async () => notify((await copyText(answerText(message))) ? 'Copied' : 'Copy failed')
  const copyRef = async () => notify((await copyText(response.request_id)) ? 'Copied' : 'Copy failed')

  let body
  if (status === 'pending') {
    body = (
      <div className="w-full rounded-2xl border border-line bg-surface p-5" role="status">
        <div className="shimmer h-4 w-2/3 rounded" />
        <div className="shimmer mt-3 h-4 w-1/2 rounded" />
        <p className="mt-4 text-xs text-muted">Working on your answer…</p>
      </div>
    )
  } else if (status === 'chat') {
    body = (
      <div className="max-w-xl whitespace-pre-wrap break-words rounded-2xl rounded-tl-sm border border-line bg-surface px-4 py-2.5 text-sm text-ink shadow-sm">
        {response.message}
      </div>
    )
  } else if (status === 'confirmation') {
    body = (
      <div className="flex w-full max-w-4xl flex-col gap-3 rounded-2xl rounded-tl-sm border border-line bg-surface px-4 py-3 text-sm text-ink shadow-sm">
        <p>{response.message}</p>
        {response.confirmation_sql && (
          <pre className="max-h-64 overflow-auto rounded-xl border border-line bg-surface-high p-3 text-xs text-ink">
            <code>{response.confirmation_sql}</code>
          </pre>
        )}
      </div>
    )
  } else if (status === 'clarification') {
    body = (
      <div className="flex w-full max-w-4xl flex-col gap-3 rounded-2xl rounded-tl-sm border border-line bg-surface px-4 py-3 text-sm text-ink shadow-sm">
        <p>{response.message}</p>
        {response.clarification_options?.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {response.clarification_options.map((option) => (
              <button
                key={option}
                type="button"
                disabled={!canRetry || !isCurrent}
                aria-disabled={!canRetry || !isCurrent}
                onClick={() => onAskFollowUp(option)}
                className="rounded-xl border border-line bg-surface-high px-3 py-2 text-xs text-ink transition-colors hover:border-primary/50 hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50"
              >
                {option}
              </button>
            ))}
          </div>
        )}
      </div>
    )
  } else if (status === 'ok') {
    body = (
      <>
        {summary && (
          <p className="max-w-4xl text-sm leading-relaxed text-ink">{summary}</p>
        )}
        <QueryResultTable
          response={response}
          notify={notify}
          question={message.question}
          onUseSuggestion={onUseSuggestion}
          elapsedSeconds={cacheHit ? null : secs}
        />
        {followUps.length > 0 && (
          <section className="w-full max-w-4xl" aria-label="Suggested follow-up questions">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-medium text-muted">You could also ask</h3>
              <button
                type="button"
                onClick={() => setFollowUpsDismissed(true)}
                className="rounded px-2 py-1 text-xs text-muted hover:bg-surface-high hover:text-ink"
                aria-label="Dismiss follow-up suggestions"
              >
                Dismiss
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              {followUps.map((question) => (
                <button
                  key={question}
                  type="button"
                  disabled={!canRetry}
                  onClick={() => onAskFollowUp(question)}
                  className="rounded-xl border border-line bg-surface px-3 py-2 text-left text-xs text-ink transition-colors hover:border-primary/50 hover:bg-surface-high disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {question}
                </button>
              ))}
            </div>
          </section>
        )}
      </>
    )
  } else if (status === 'unanswerable') {
    body = (
      <div className="flex w-full items-start gap-3 rounded-2xl border border-line bg-surface p-5">
        <Icon name="info" size={20} className="mt-0.5 text-accent" />
        <div>
          <p className="text-sm font-semibold text-ink">I can't answer that from this database.</p>
          {response.message && <p className="mt-1 text-xs text-muted">{response.message}</p>}
        </div>
      </div>
    )
  } else if (status === 'escalated') {
    body = (
      <div className="flex w-full flex-col gap-4 rounded-2xl border border-warn/40 bg-warn/10 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <Icon name="support_agent" size={22} className="mt-0.5 text-warn" />
          <div>
            <p className="text-sm font-semibold text-ink">{response.message || 'This question needs a human to review it.'}</p>
            <p className="mt-1 text-xs text-muted">Share this ID with support.</p>
          </div>
        </div>
        {response.request_id && (
          <div className="flex shrink-0 items-center gap-2 rounded-xl border border-warn/30 bg-surface px-3 py-1.5">
            <span className="text-xs text-muted">Reference ID:</span>
            <code className="font-mono text-xs font-semibold text-ink">{response.request_id}</code>
            <button
              type="button"
              onClick={copyRef}
              className="inline-flex items-center gap-1 rounded-md border border-warn/30 bg-warn/15 px-2 py-0.5 text-xs font-medium text-ink hover:bg-warn/25"
            >
              <Icon name="content_copy" size={13} />
              Copy
            </button>
          </div>
        )}
      </div>
    )
  } else if (status === 'stopped') {
    body = (
      <div className="flex w-full items-center justify-between gap-3 rounded-2xl border border-line bg-surface p-4 text-sm text-muted">
        <span>You stopped this request.</span>
        <button type="button" disabled={!canRetry} onClick={() => onRetry(message.id)} className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-ink hover:bg-surface-high disabled:opacity-40">
          Try again
        </button>
      </div>
    )
  } else {
    body = (
      <div className="flex w-full flex-col gap-3 rounded-2xl border border-bad/40 bg-bad/10 p-5 sm:flex-row sm:items-center sm:justify-between" role="alert">
        <div className="flex items-start gap-3">
          <Icon name="error" size={20} className="mt-0.5 text-bad" />
          <p className="text-sm text-ink">{error?.message || 'Something went wrong. Please try again.'}</p>
        </div>
        <button type="button" disabled={!canRetry} onClick={() => onRetry(message.id)} className="shrink-0 rounded-lg border border-bad/40 bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:bg-surface-high disabled:opacity-40">
          Try again
        </button>
      </div>
    )
  }

  const showActions = ['ok', 'unanswerable', 'escalated'].includes(status)

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col items-start gap-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <div className={`flex h-5 w-5 items-center justify-center rounded-md border text-[11px] font-bold ${warn ? 'border-warn/30 bg-warn/20 text-warn' : bad ? 'border-bad/30 bg-bad/20 text-bad' : 'border-primary/30 bg-primary/20 text-primary'}`}>
          AI
        </div>
        <span className="font-medium text-ink">Ask Your Data</span>
        <span>{time(message.timestamp)}</span>
        {cacheHit && (
          <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent">
            <Icon name="bolt" size={13} />
            Instant answer from cache
          </span>
        )}
        {status === 'ok' && !response?.results?.length && secs !== null && !cacheHit && <span>Answered in {secs.toFixed(1)}s</span>}
      </div>

      {body}

      {showActions && (
        <div className="flex items-center gap-0.5">
          <ActionButton icon="content_copy" label="Copy answer" onClick={copyAnswer} />
          <ActionButton icon="refresh" label="Ask again" onClick={() => onRetry(message.id)} disabled={!canRetry} />
          <ActionButton icon="thumb_up" label="Helpful" active={message.feedback === 'up'} onClick={() => onFeedback(message.id, 'up')} />
          <ActionButton icon="thumb_down" label="Not helpful" active={message.feedback === 'down'} onClick={() => onFeedback(message.id, 'down')} />
        </div>
      )}
    </div>
  )
}

export default function MessageBubble({ message, ...props }) {
  return message.role === 'user' ? <UserMessage message={message} /> : <AssistantMessage message={message} {...props} />
}
