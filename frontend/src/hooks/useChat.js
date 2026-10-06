import { useCallback, useRef, useState } from 'react'
import { ApiError, streamChat } from '../api/chatApi.js'
import { applyEvent, finalizePipeline, newPipeline } from '../utils/pipeline.js'
export const MAX_LEN = 1000
const TIMEOUT_MS = 100_000
const uid = () => crypto.randomUUID()
const makeChat = () => ({ id: uid(), title: 'New chat', createdAt: Date.now(), messages: [] })
const shorten = (s, n = 40) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

/**
 * All chat state lives here (memory only).
 * chat    = { id, title, createdAt, messages[] }
 * message = user:      { id, role:'user', text, timestamp }
 *           assistant: { id, role:'assistant', question, status, response, error, pipeline, feedback, timestamp }
 *           status: 'pending' | 'ok' | 'unanswerable' | 'escalated' | 'error' | 'stopped'
 */


export default function useChat() {
  const [initialChat] = useState(makeChat)
  const [chats, setChats] = useState([initialChat])
  const [activeChatId, setActiveChatId] = useState(initialChat.id)
  const [draft, setDraft] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [runningChatId, setRunningChatId] = useState(null)
  const [inlineError, setInlineError] = useState(null)

  const abortRef = useRef(null)
  const loadingRef = useRef(false)
  const flagsRef = useRef({ timedOut: false })

  const activeChat = chats.find((c) => c.id === activeChatId) || chats[0]

  const patchMessage = useCallback((chatId, msgId, fn) => {
    setChats((prev) =>
      prev.map((c) => (c.id !== chatId ? c : { ...c, messages: c.messages.map((m) => (m.id === msgId ? fn(m) : m)) })),
    )
  }, [])

  const updateDraft = useCallback((value) => {
    setDraft(value)
    setInlineError(null)
  }, [])

  const sendQuestion = useCallback(
    async (text, clear = false, suggestionId = null) => {
      const question = (text || '').trim()
      if (!question || loadingRef.current) return
      if (question.length > MAX_LEN) {
        setInlineError(`Please keep your question under ${MAX_LEN} characters.`)
        return
      }

      const chatId = activeChatId
      const userMsg = { id: uid(), role: 'user', text: question, timestamp: Date.now() }
      const aiMsg = {
        id: uid(),
        role: 'assistant',
        question,
        status: 'pending',
        response: null,
        error: null,
        pipeline: newPipeline(),
        feedback: null,
        timestamp: Date.now(),
      }
      setChats((prev) =>
        prev.map((c) =>
          c.id !== chatId
            ? c
            : { ...c, title: c.messages.length === 0 ? shorten(question) : c.title, messages: [...c.messages, userMsg, aiMsg] },
        ),
      )

      const ctrl = new AbortController()
      abortRef.current = ctrl
      flagsRef.current = { timedOut: false }
      loadingRef.current = true
      setIsLoading(true)
      setRunningChatId(chatId)
      setInlineError(null)
      if (clear) setDraft('')
      const timer = setTimeout(() => {
        flagsRef.current.timedOut = true
        ctrl.abort()
      }, TIMEOUT_MS)

      const finish = (patch, outcome, fallback = false) =>
        patchMessage(chatId, aiMsg.id, (m) => ({ ...m, ...patch, pipeline: finalizePipeline(m.pipeline, { outcome, fallback }) }))

      let result = null
      try {
        await streamChat({
          question,
          conversationId: chatId,
          suggestionId,
          signal: ctrl.signal,
          onEvent: (ev) => {
            if (ev.type === 'result') {
              result = ev
              return
            }
            patchMessage(chatId, aiMsg.id, (m) => ({ ...m, pipeline: applyEvent(m.pipeline, ev) }))
          },
        })
        if (!result) throw new ApiError('server', 'No answer was returned. Please try again.')
        const body = { ...result }
        delete body.type
        const fallback = Boolean(body.fallback)
        delete body.fallback
        const status = ['ok', 'unanswerable', 'escalated', 'chat'].includes(body.status) ? body.status : 'escalated'
        finish({ status, response: body }, status, fallback)
      } catch (err) {
                console.error('chat request failed', err)   // <- add here
        if (err?.name === 'AbortError') {
          if (flagsRef.current.timedOut) {
            finish({ status: 'error', error: { kind: 'timeout', message: 'This is taking longer than expected. Please try again.' } }, 'failed')
            if (clear) setDraft((d) => d || question)
          } else {
            finish({ status: 'stopped' }, 'stopped')
          }
        } else if (err instanceof ApiError && err.kind === 'validation') {
          // Remove the two messages we just added and show the message under the composer.
          setChats((prev) =>
            prev.map((c) => (c.id !== chatId ? c : { ...c, messages: c.messages.filter((m) => m.id !== userMsg.id && m.id !== aiMsg.id) })),
          )
          setInlineError(err.message)
          setDraft((d) => d || question)
        } else {
          const message = err instanceof ApiError ? err.message : 'Something went wrong. Please try again.'
          finish({ status: 'error', error: { kind: err?.kind || 'server', message } }, 'failed')
          if (clear) setDraft((d) => d || question)
        }
      } finally {
        clearTimeout(timer)
        abortRef.current = null
        loadingRef.current = false
        setIsLoading(false)
        setRunningChatId(null)
      }
    },
    [activeChatId, patchMessage],
  )

  const stop = useCallback(() => abortRef.current?.abort(), [])

  const retry = useCallback(
    (messageId) => {
      const msg = activeChat.messages.find((m) => m.id === messageId)
      if (msg?.question) sendQuestion(msg.question)
    },
    [activeChat, sendQuestion],
  )

  const feedback = useCallback(
    (messageId, value) => {
      console.log('feedback', { messageId, value })
      patchMessage(activeChatId, messageId, (m) => ({ ...m, feedback: m.feedback === value ? null : value }))
    },
    [activeChatId, patchMessage],
  )

  const createChat = useCallback(() => {
    setInlineError(null)
    const empty = chats.find((c) => c.messages.length === 0)
    if (empty) {
      setActiveChatId(empty.id)
      return
    }
    const chat = makeChat()
    setChats((prev) => [chat, ...prev])
    setActiveChatId(chat.id)
  }, [chats])

  const selectChat = useCallback((id) => {
    setInlineError(null)
    setActiveChatId(id)
  }, [])

  const renameChat = useCallback((id, title) => {
    const t = title.trim()
    if (!t) return
    setChats((prev) => prev.map((c) => (c.id === id ? { ...c, title: t.slice(0, 80) } : c)))
  }, [])

  const deleteChat = useCallback(
    (id) => {
      if (id === runningChatId) abortRef.current?.abort()
      const next = chats.filter((c) => c.id !== id)
      const remaining = next.length ? next : [makeChat()]
      if (id === activeChatId) setActiveChatId(remaining[0].id)
      setChats(remaining)
    },
    [activeChatId, chats, runningChatId],
  )

  return {
    chats, activeChat, activeChatId, draft, updateDraft, isLoading, runningChatId, inlineError,
    sendQuestion, stop, retry, feedback, createChat, selectChat, renameChat, deleteChat,
  }
}
