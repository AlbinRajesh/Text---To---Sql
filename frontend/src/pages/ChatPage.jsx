import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { checkHealth } from '../api/chatApi.js'
import ChatWindow from '../components/ChatWindow.jsx'
import Icon from '../components/Icon.jsx'
import PipelinePanel from '../components/PipelinePanel.jsx'
import Sidebar from '../components/Sidebar.jsx'
import useChat, { MAX_LEN } from '../hooks/useChat.js'
import { replaceUniqueText } from '../utils/csv.js'

export default function ChatPage() {
  const chat = useChat()
  const { activeChat, createChat, sendQuestion } = chat

  const [theme, setTheme] = useState('dark')
  const [healthOk, setHealthOk] = useState(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false) // mobile drawer
  const [pipelineOpen, setPipelineOpen] = useState(false) // drawer below xl
  const [toast, setToast] = useState(null)
  const toastTimer = useRef(null)

  // Theme
  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  // Health check (real /api/health only)
  useEffect(() => {
    let alive = true
    const run = async () => {
      const ok = await checkHealth()
      if (alive) setHealthOk(ok)
    }
    run()
    const t = setInterval(run, 30_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  // Toast
  const notify = useCallback((message) => {
    setToast(message)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 1800)
  }, [])
  const useValueSuggestion = useCallback((question, suggestion, revisedQuestion) => {
    const safeReplacement = replaceUniqueText(
      question,
      suggestion.original,
      suggestion.suggested,
    )
    if (!safeReplacement || safeReplacement !== revisedQuestion) {
      notify('Could not safely update the draft. Please edit the suggested value manually.')
      return
    }
    if (suggestion.suggestion_id) {
      sendQuestion(safeReplacement, true, suggestion.suggestion_id)
      return
    }
    sendQuestion(safeReplacement, true)
  }, [notify, sendQuestion])
  useEffect(() => () => clearTimeout(toastTimer.current), [])

  // Shortcuts: Ctrl/Cmd+K = new chat, Esc = close drawers
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        createChat()
      } else if (e.key === 'Escape') {
        setSidebarOpen(false)
        setPipelineOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [createChat])

  // Pipeline shown = the latest AI message in the open chat
  const pipeline = useMemo(() => {
    const ms = activeChat.messages
    for (let i = ms.length - 1; i >= 0; i--) if (ms[i].role === 'assistant') return ms[i].pipeline
    return null
  }, [activeChat])
  const generatedSql = useMemo(() => {
    const ms = activeChat.messages
    for (let i = ms.length - 1; i >= 0; i--) {
      if (ms[i].role === 'assistant') return ms[i].generatedSql
    }
    return null
  }, [activeChat])

  return (
    <div className="flex h-screen flex-col bg-bg text-ink">
      <header className="z-40 flex h-14 shrink-0 items-center justify-between border-b border-line bg-bg/90 px-4 backdrop-blur-xl">
        <div className="flex min-w-0 items-center gap-3">
          <button type="button" onClick={() => setSidebarOpen(true)} aria-label="Open chats" className="rounded-lg p-1.5 text-muted hover:bg-surface hover:text-ink md:hidden">
            <Icon name="menu" size={20} />
          </button>
          {sidebarCollapsed && (
            <button type="button" onClick={() => setSidebarCollapsed(false)} aria-label="Show sidebar" title="Show sidebar" className="hidden rounded-lg p-1.5 text-muted hover:bg-surface hover:text-ink md:block">
              <Icon name="dock_to_right" size={20} />
            </button>
          )}
          <span className="truncate text-sm font-medium text-ink">{activeChat.messages.length ? activeChat.title : 'New chat'}</span>
        </div>
        <button
          type="button"
          onClick={() => setPipelineOpen(true)}
          className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs text-ink transition-colors hover:bg-surface-high xl:hidden"
        >
          <Icon name="account_tree" size={16} className="text-accent" />
          Steps
        </button>
      </header>

      {/* 1. Updated row that holds the sidebar, chat, and panel (added min-h-0) */}
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {sidebarOpen && <div className="fixed inset-0 z-40 bg-black/60 md:hidden" onClick={() => setSidebarOpen(false)} />}
        {pipelineOpen && <div className="fixed inset-0 z-40 bg-black/60 xl:hidden" onClick={() => setPipelineOpen(false)} />}

        <Sidebar
          chats={chat.chats}
          activeChatId={chat.activeChatId}
          runningChatId={chat.runningChatId}
          mobileOpen={sidebarOpen}
          collapsed={sidebarCollapsed}
          onNew={chat.createChat}
          onSelect={chat.selectChat}
          onRename={chat.renameChat}
          onDelete={chat.deleteChat}
          onCollapse={() => setSidebarCollapsed(true)}
          onCloseMobile={() => setSidebarOpen(false)}
          healthOk={healthOk}
          theme={theme}
          onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
        />

        {/* 2. Fixed parser error: properly opened and closed <main> tag with requested classes */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <ChatWindow
            chat={activeChat}
            draft={chat.draft}
            onDraftChange={chat.updateDraft}
            onSend={chat.sendQuestion}
            onStop={chat.stop}
            isLoading={chat.isLoading}
            inlineError={chat.inlineError}
            onRetry={chat.retry}
            onFeedback={chat.feedback}
            onUseSuggestion={useValueSuggestion}
            notify={notify}
            maxLen={MAX_LEN}
          />
        </main>

        <PipelinePanel
          pipeline={pipeline}
          generatedSql={generatedSql}
          open={pipelineOpen}
          onClose={() => setPipelineOpen(false)}
        />
      </div>

      {toast && (
        <div role="status" className="toast-in fixed bottom-24 left-1/2 z-[60] -translate-x-1/2 rounded-full border border-line bg-surface-high px-4 py-2 text-xs font-medium text-ink shadow-xl">
          {toast}
        </div>
      )}
    </div>
  )
}