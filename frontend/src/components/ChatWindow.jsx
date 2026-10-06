import { useEffect, useLayoutEffect, useRef } from 'react'
import Icon from './Icon.jsx'
import MessageBubble from './MessageBubble.jsx'

const EXAMPLES = [
  'How many plans do we have in total?',
  'List plans with a price greater than 500',
  'What is the average price of all plans?',
  'Show me all plans',
]

export default function ChatWindow({
  chat, draft, onDraftChange, onSend, onStop, isLoading, inlineError, onRetry, onFeedback,
  onUseSuggestion, notify, maxLen,
}) {
  const scrollRef = useRef(null)
  const stickRef = useRef(true)
  const textareaRef = useRef(null)
  const lastChatRef = useRef(chat.id)

  // Auto-scroll to newest message, but not if the person scrolled up.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (lastChatRef.current !== chat.id) {
      lastChatRef.current = chat.id
      stickRef.current = true
    }
    if (stickRef.current) el.scrollTo({ top: el.scrollHeight })
  }, [chat.messages, chat.id])

  const onScroll = () => {
    const el = scrollRef.current
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  // Auto-growing textarea (max ~8 lines).
  useLayoutEffect(() => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 180)}px`
  }, [draft])

  const trimmed = draft.trim()
  const overLimit = draft.length > maxLen
  const canSend = trimmed.length > 0 && !overLimit && !isLoading

  const submit = () => {
    if (canSend) onSend(draft, true)
  }

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  const counterColor = overLimit ? 'text-bad' : draft.length > maxLen * 0.9 ? 'text-warn' : 'text-muted'

  return (
    <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 space-y-7 overflow-y-auto px-4 py-6 md:px-8" aria-live="polite">
      <div ref={scrollRef} onScroll={onScroll} className="flex-1 space-y-7 overflow-y-auto px-4 py-6 md:px-8" aria-live="polite">
        {chat.messages.length === 0 ? (
          <div className="mx-auto flex h-full max-w-2xl flex-col items-center justify-center gap-6 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-primary to-accent text-white shadow-lg">
              <Icon name="query_stats" size={26} />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-ink">What would you like to know?</h1>
              <p className="mt-2 text-sm text-muted">Ask a question about your business data in plain English.</p>
            </div>
            <div className="grid w-full gap-2 sm:grid-cols-2">
              {EXAMPLES.map((q) => (
                <button
                  key={q}
                  type="button"
                  disabled={isLoading}
                  onClick={() => onSend(q, false)}
                  className="rounded-xl border border-line bg-surface p-3 text-left text-sm text-ink transition-colors hover:border-primary/50 hover:bg-surface-high disabled:opacity-50"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          chat.messages.map((m) => (
            <MessageBubble
              key={m.id}
              message={m}
              onRetry={onRetry}
              onFeedback={onFeedback}
              onUseSuggestion={onUseSuggestion}
              notify={notify}
              canRetry={!isLoading}
            />
          ))
        )}
      </div>

      <div className="sticky bottom-0 z-30 border-t border-line bg-bg/95 px-4 py-3 backdrop-blur-md md:px-8">
        <div className="mx-auto flex max-w-4xl flex-col gap-2">
          <div className="rounded-2xl border border-line bg-surface p-3 shadow-xl transition-all focus-within:border-primary/60 focus-within:shadow-[0_0_28px_-6px_rgba(99,102,241,0.55)]">
            <div className="flex items-start gap-2.5">
              <Icon name="psychology" size={20} className="mt-1 shrink-0 text-primary" />
              <textarea
                ref={textareaRef}
                value={draft}
                rows={1}
                onChange={(e) => onDraftChange(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Ask a question about your business data…"
                aria-label="Your question"
                aria-invalid={Boolean(inlineError) || overLimit}
                className="w-full resize-none bg-transparent font-sans text-sm leading-relaxed text-ink outline-none placeholder:text-muted"
              />
            </div>
            <div className="mt-1 flex items-center justify-between border-t border-line/60 pt-2">
              <div className="flex items-center gap-3 text-xs">
                <span className={`font-mono ${counterColor}`}>{draft.length} / {maxLen}</span>
                <span className="hidden text-muted sm:inline">Enter to send · Shift+Enter for a new line</span>
              </div>
              <div className="flex items-center gap-2">
                {isLoading && (
                  <button
                    type="button"
                    onClick={onStop}
                    className="flex items-center gap-1 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-bad transition-colors hover:bg-surface-high"
                  >
                    <Icon name="stop_circle" size={15} />
                    Stop
                  </button>
                )}
                <button
                  type="button"
                  onClick={submit}
                  disabled={!canSend}
                  className="flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-primary to-indigo-600 px-4 py-1.5 text-xs font-semibold text-white shadow-md transition-transform hover:opacity-95 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isLoading ? (
                    'Working…'
                  ) : (
                    <>
                      Send
                      <Icon name="arrow_upward" size={15} />
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>

          {inlineError && (
            <p role="alert" className="px-1 text-xs text-bad">
              {inlineError}
            </p>
          )}
          {overLimit && !inlineError && (
            <p role="alert" className="px-1 text-xs text-bad">
              Please keep your question under {maxLen} characters.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
