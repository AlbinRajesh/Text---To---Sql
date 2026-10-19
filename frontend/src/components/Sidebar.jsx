import { useMemo, useRef, useState } from 'react'
import Icon from './icon.jsx'

const GROUPS = ['Today', 'Yesterday', 'Earlier']

function groupOf(ts) {
  const now = new Date()
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  if (ts >= startToday) return 'Today'
  if (ts >= startToday - 86_400_000) return 'Yesterday'
  return 'Earlier'
}

function timeLabel(ts) {
  const d = new Date(ts)
  return groupOf(ts) === 'Earlier'
    ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export default function Sidebar({
  chats, activeChatId, runningChatId, mobileOpen, collapsed,
  onNew, onSelect, onRename, onDelete, onCollapse, onCloseMobile,
  healthOk, theme, onToggleTheme,
}) {
  const [query, setQuery] = useState('')
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('')
  const listRef = useRef(null)

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase()
    const items = chats
      .filter((c) => c.messages.length > 0 && (!q || c.title.toLowerCase().includes(q)))
      .sort((a, b) => b.createdAt - a.createdAt)
    return GROUPS.map((g) => ({ name: g, items: items.filter((c) => groupOf(c.createdAt) === g) })).filter((g) => g.items.length)
  }, [chats, query])

  const hasChats = chats.some((c) => c.messages.length > 0)

  const startEdit = (chat) => {
    setEditingId(chat.id)
    setEditValue(chat.title)
  }
  const commitEdit = () => {
    if (editingId) onRename(editingId, editValue)
    setEditingId(null)
  }

  const onItemKeyDown = (e, id) => {
    if (e.target !== e.currentTarget) return
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSelect(id)
      onCloseMobile()
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const items = [...listRef.current.querySelectorAll('[data-chat-item]')]
      items[items.indexOf(e.currentTarget) + (e.key === 'ArrowDown' ? 1 : -1)]?.focus()
    }
  }

  const status =
    healthOk === null
      ? { text: 'Checking…', dot: 'bg-muted' }
      : healthOk
        ? { text: 'Connected', dot: 'bg-ok' }
        : { text: 'Disconnected', dot: 'bg-bad' }

  return (
    <aside
      className={`fixed inset-y-0 left-0 z-50 flex w-[260px] flex-col border-r border-line bg-bg transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${
        mobileOpen ? 'translate-x-0' : '-translate-x-full'
      } ${collapsed ? 'md:hidden' : ''}`}
      aria-label="Chats"
    >
      <div className="flex flex-col gap-3.5 border-b border-line p-4">
        <div className="flex items-center justify-between">
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white">
              <Icon name="query_stats" size={18} />
            </div>
            <div className="flex min-w-0 flex-col">
              <span className="truncate text-sm font-bold tracking-tight text-ink">Ask Your Data</span>
              <span className="truncate text-[10px] text-muted">Ask in plain English</span>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              onCollapse()
              onCloseMobile()
            }}
            title="Collapse sidebar"
            aria-label="Collapse sidebar"
            className="rounded-md p-1 text-muted transition-colors hover:bg-surface hover:text-ink"
          >
            <Icon name="menu_open" size={18} />
          </button>
        </div>

        <button
          type="button"
          onClick={() => {
            onNew()
            onCloseMobile()
          }}
          className="flex w-full items-center justify-between rounded-lg bg-gradient-to-r from-primary to-indigo-600 px-3 py-2 text-xs font-semibold text-white shadow-sm transition-all duration-150 hover:opacity-95 active:scale-95"
        >
          <span className="flex items-center gap-2">
            <Icon name="add" size={16} />
            New chat
          </span>
          <kbd className="rounded bg-black/25 px-1.5 py-0.5 font-mono text-[10px]">Ctrl K</kbd>
        </button>

        <div className="relative flex items-center">
          <Icon name="search" size={16} className="pointer-events-none absolute left-2.5 text-muted" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search chats"
            aria-label="Search chats"
            className="w-full rounded-lg border border-line bg-surface py-1.5 pl-8 pr-3 text-xs text-ink outline-none placeholder:text-muted focus:border-primary/60"
          />
        </div>
      </div>

      <div ref={listRef} className="flex-1 space-y-4 overflow-y-auto px-3 py-3">
        {!hasChats && <p className="px-2 text-xs text-muted">No chats yet. Ask your first question.</p>}
        {hasChats && grouped.length === 0 && <p className="px-2 text-xs text-muted">No chats match your search.</p>}

        {grouped.map((group) => (
          <div key={group.name} className="space-y-1">
            <div className="px-2 text-[11px] font-semibold text-muted">{group.name}</div>
            {group.items.map((chat) => {
              const active = chat.id === activeChatId
              const running = chat.id === runningChatId
              return (
                <div
                  key={chat.id}
                  data-chat-item
                  role="button"
                  tabIndex={0}
                  aria-current={active ? 'true' : undefined}
                  onClick={() => {
                    onSelect(chat.id)
                    onCloseMobile()
                  }}
                  onKeyDown={(e) => onItemKeyDown(e, chat.id)}
                  className={`group flex cursor-pointer items-center justify-between rounded-lg border p-2 text-xs transition-colors ${
                    active ? 'border-primary/40 bg-surface text-ink shadow-sm' : 'border-transparent text-muted hover:bg-surface hover:text-ink'
                  }`}
                >
                  <div className="min-w-0 flex-1 pr-1">
                    {editingId === chat.id ? (
                      <input
                        autoFocus
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        onClick={(e) => e.stopPropagation()}
                        onBlur={commitEdit}
                        onKeyDown={(e) => {
                          e.stopPropagation()
                          if (e.key === 'Enter') commitEdit()
                          if (e.key === 'Escape') setEditingId(null)
                        }}
                        aria-label="Chat name"
                        className="w-full rounded border border-primary/60 bg-bg px-1.5 py-0.5 text-xs text-ink outline-none"
                      />
                    ) : (
                      <>
                        <div className="flex items-center gap-1.5">
                          {running && <Icon name="sync" size={12} spin className="text-accent" />}
                          <span className={`truncate ${active ? 'font-medium' : ''}`}>{chat.title}</span>
                        </div>
                        <span className="text-[10px] text-muted">{timeLabel(chat.createdAt)}</span>
                      </>
                    )}
                  </div>
                  {editingId !== chat.id && (
                    <div className="hidden shrink-0 items-center gap-0.5 focus-within:flex group-hover:flex">
                      <button
                        type="button"
                        title="Rename"
                        aria-label="Rename chat"
                        onClick={(e) => {
                          e.stopPropagation()
                          startEdit(chat)
                        }}
                        className="rounded p-1 text-muted hover:bg-surface-high hover:text-ink"
                      >
                        <Icon name="edit" size={13} />
                      </button>
                      <button
                        type="button"
                        title="Delete"
                        aria-label="Delete chat"
                        onClick={(e) => {
                          e.stopPropagation()
                          if (window.confirm('Delete this chat?')) onDelete(chat.id)
                        }}
                        className="rounded p-1 text-muted hover:bg-surface-high hover:text-bad"
                      >
                        <Icon name="delete" size={13} />
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ))}
      </div>

      <div className="flex flex-col gap-2.5 border-t border-line bg-surface/40 p-3 text-xs">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2" role="status">
            <span className={`h-2 w-2 rounded-full ${status.dot}`} />
            <span className="text-[11px] font-medium text-ink">{status.text}</span>
          </div>
          <button
            type="button"
            onClick={onToggleTheme}
            title="Switch theme"
            aria-label="Switch theme"
            className="flex h-7 w-7 items-center justify-center rounded text-muted transition-colors hover:bg-surface hover:text-ink"
          >
            <Icon name={theme === 'dark' ? 'light_mode' : 'dark_mode'} size={16} />
          </button>
        </div>
        <div className="flex items-center gap-1.5 text-[10px] text-muted">
          <Icon name="lock" size={12} className="text-ok" />
          <span>Read-only. Your data is never modified.</span>
        </div>
      </div>
    </aside>
  )
}
