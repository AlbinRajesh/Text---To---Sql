import { useCallback, useState } from 'react'
import Icon from '../components/icon.jsx'
import {
  createBusinessRuleDraft,
  exportBusinessRules,
  getBusinessRuleDraft,
  listBusinessRuleRevisions,
  publishBusinessRuleDraft,
} from '../api/adminBusinessRules.js'

const DUPLICATE_RULEBOOK_VERSION_ERROR =
  'Increment _meta.version before creating a rulebook draft.'
const METADATA_BLOCK =
  /(^## Metadata\s*\r?\n\s*```json\s*\r?\n)([\s\S]*?)(\r?\n\s*```)/m

function readRulebookVersion(markdown) {
  const match = METADATA_BLOCK.exec(markdown)
  if (!match) return null
  try {
    const metadata = JSON.parse(match[2])
    return typeof metadata?._meta?.version === 'string'
      ? metadata._meta.version
      : null
  } catch {
    return null
  }
}

function setRulebookVersion(markdown, version) {
  const match = METADATA_BLOCK.exec(markdown)
  if (!match) return null
  try {
    const metadata = JSON.parse(match[2])
    if (!metadata?._meta || typeof metadata._meta.version !== 'string') return null
    metadata._meta.version = version
    return markdown.replace(
      METADATA_BLOCK,
      (_block, prefix, _oldMetadata, suffix) =>
        `${prefix}${JSON.stringify(metadata, null, 2)}${suffix}`,
    )
  } catch {
    return null
  }
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function incrementRulebookVersion(markdown) {
  const currentVersion = readRulebookVersion(markdown)
  if (!currentVersion) return null
  const semver = /^(\d+)\.(\d+)\.(\d+)$/.exec(currentVersion)
  const draftSuffix = /^(.*?\.draft\.)(\d+)$/.exec(currentVersion)
  const nextVersion = semver
    ? `${semver[1]}.${semver[2]}.${Number(semver[3]) + 1}`
    : draftSuffix
      ? `${draftSuffix[1]}${Number(draftSuffix[2]) + 1}`
      : `${currentVersion}.draft.1`
  const updatedMarkdown = setRulebookVersion(markdown, nextVersion)
  if (!updatedMarkdown) return null

  return {
    markdown: updatedMarkdown,
    currentVersion,
    nextVersion,
  }
}

function ChangeGroup({ title, changes }) {
  if (!changes) return null
  const groups = [
    ['Added', changes.added],
    ['Changed', changes.changed],
    ['Removed', changes.removed],
  ].filter(([, values]) => values?.length)

  return (
    <div className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h4>
      {groups.length === 0 ? (
        <p className="text-sm text-muted">No changes</p>
      ) : (
        groups.map(([label, values]) => (
          <div key={label} className="text-sm">
            <span className="font-medium text-ink">{label}: </span>
            <span className="break-words text-muted">{values.join(', ')}</span>
          </div>
        ))
      )}
    </div>
  )
}

export default function AdminBusinessRulesPage({ onNavigate }) {
  const [key, setKey] = useState('')
  const [connected, setConnected] = useState(false)
  const [markdown, setMarkdown] = useState('')
  const [fileName, setFileName] = useState('')
  const [revisions, setRevisions] = useState([])
  const [selectedDraft, setSelectedDraft] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const refreshRevisions = useCallback(async (adminKey) => {
    const items = await listBusinessRuleRevisions(adminKey)
    setRevisions(items)
  }, [])

  const handleConnect = async (event) => {
    event.preventDefault()
    if (!key.trim()) {
      setError('Enter the ADMIN_API_KEY to connect.')
      return
    }
    setBusy('connect')
    setError('')
    setNotice('')
    try {
      const current = await exportBusinessRules(key)
      setMarkdown(current)
      setFileName('Current server rulebook')
      setConnected(true)
      try {
        await refreshRevisions(key)
      } catch (revisionError) {
        setError(`Connected, but revision history could not be loaded: ${revisionError.message}`)
      }
    } catch (connectError) {
      setConnected(false)
      setError(connectError.message)
    } finally {
      setBusy('')
    }
  }

  const handleDisconnect = () => {
    setKey('')
    setConnected(false)
    setMarkdown('')
    setFileName('')
    setSelectedDraft(null)
    setRevisions([])
    setError('')
    setNotice('Disconnected. The key was cleared from this page.')
  }

  const handleExport = async () => {
    if (
      markdown.trim()
      && fileName !== 'Current server rulebook'
      && !window.confirm('Replace the editor contents with the current server rulebook?')
    ) {
      return
    }
    setBusy('export')
    setError('')
    setNotice('')
    try {
      const current = await exportBusinessRules(key)
      setMarkdown(current)
      setFileName('Current server rulebook')
      const url = URL.createObjectURL(new Blob([current], { type: 'text/markdown' }))
      const link = document.createElement('a')
      link.href = url
      link.download = 'business-rules.md'
      link.click()
      URL.revokeObjectURL(url)
      setNotice('Current rules exported and loaded into the editor.')
    } catch (exportError) {
      setError(exportError.message)
    } finally {
      setBusy('')
    }
  }

  const handleFile = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError('')
    setNotice('')
    try {
      const contents = await file.text()
      setMarkdown(contents)
      setFileName(file.name)
      setSelectedDraft(null)
    } catch (fileError) {
      setError(`Could not read the selected file: ${fileError.message}`)
    }
  }

  const handleCreateDraft = async () => {
    setBusy('draft')
    setError('')
    setNotice('')
    try {
      let candidateMarkdown = markdown
      let versionChange = null
      let draft
      try {
        draft = await createBusinessRuleDraft(key, candidateMarkdown)
      } catch (draftError) {
        if (
          draftError.status !== 422
          || draftError.message !== DUPLICATE_RULEBOOK_VERSION_ERROR
        ) {
          throw draftError
        }
        versionChange = incrementRulebookVersion(candidateMarkdown)
        if (!versionChange) throw draftError

        candidateMarkdown = versionChange.markdown
        setMarkdown(candidateMarkdown)
        draft = await createBusinessRuleDraft(key, candidateMarkdown)
      }
      const submittedVersion = readRulebookVersion(candidateMarkdown)
      const savedVersion = draft.preview?.rulebook_version
      if (
        typeof savedVersion === 'string'
        && submittedVersion
        && savedVersion !== submittedVersion
      ) {
        const updatedMarkdown = setRulebookVersion(
          candidateMarkdown,
          savedVersion,
        )
        if (updatedMarkdown) {
          candidateMarkdown = updatedMarkdown
          setMarkdown(candidateMarkdown)
          versionChange = {
            currentVersion: submittedVersion,
            nextVersion: savedVersion,
          }
        }
      }
      setSelectedDraft(draft)
      const versionNotice = versionChange
        ? ` Version ${versionChange.currentVersion} matched the active rulebook, so it was incremented to ${versionChange.nextVersion}.`
        : ''
      setNotice(
        `Draft ${draft.revision_id} created. It is not published.${versionNotice}`,
      )
      try {
        await refreshRevisions(key)
      } catch (revisionError) {
        setError(`Draft created, but revision history could not be refreshed: ${revisionError.message}`)
      }
    } catch (draftError) {
      setError(draftError.message)
    } finally {
      setBusy('')
    }
  }

  const handleSelectRevision = async (revision) => {
    if (revision.status !== 'draft') return
    setBusy(`revision-${revision.revision_id}`)
    setError('')
    setNotice('')
    try {
      const details = await getBusinessRuleDraft(key, revision.revision_id)
      setSelectedDraft(details)
      setMarkdown(details.document_markdown)
      setFileName(`Draft ${revision.revision_id}`)
    } catch (revisionError) {
      setError(revisionError.message)
    } finally {
      setBusy('')
    }
  }

  const handlePublish = async () => {
    if (!selectedDraft || selectedDraft.status !== 'draft') return
    const confirmed = window.confirm(
      `Publish business-rule revision ${selectedDraft.revision_id}? This will make it active for chat requests.`,
    )
    if (!confirmed) return

    setBusy('publish')
    setError('')
    setNotice('')
    try {
      const published = await publishBusinessRuleDraft(key, selectedDraft.revision_id)
      setSelectedDraft((draft) => ({ ...draft, status: 'published' }))
      setNotice(
        `Revision ${published.revision_id} published and active (${published.term_count} terms, ${published.ontology_entity_count} ontology entities).`,
      )
      try {
        await refreshRevisions(key)
      } catch (revisionError) {
        setError(`Revision published, but history could not be refreshed: ${revisionError.message}`)
      }
    } catch (publishError) {
      setError(publishError.message)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="min-h-full bg-bg text-ink">
      <header className="sticky top-0 z-20 flex min-h-14 items-center justify-between gap-3 border-b border-line bg-bg/95 px-4 backdrop-blur md:px-8">
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white">
            <Icon name="admin_panel_settings" size={19} />
          </span>
          <div>
            <p className="text-sm font-bold">Business rules</p>
            <p className="text-[11px] text-muted">Admin tools</p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => onNavigate('/')}
          className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-xs font-medium text-ink hover:bg-surface-high"
        >
          <Icon name="chat" size={16} />
          Back to chat
        </button>
      </header>

      <main className="mx-auto max-w-6xl space-y-5 px-4 py-6 md:px-8 md:py-8">
        <section className="rounded-2xl border border-line bg-surface p-5 md:p-6">
          <div className="flex flex-col justify-between gap-4 md:flex-row md:items-center">
            <div>
              <h1 className="text-xl font-bold tracking-tight">Manage business rules</h1>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
                Export the current rulebook, prepare and validate a Markdown draft, review its changes, then explicitly publish it.
              </p>
            </div>
            {connected && (
              <button
                type="button"
                onClick={handleDisconnect}
                className="flex shrink-0 items-center gap-2 self-start rounded-lg border border-line px-3 py-2 text-xs text-muted hover:bg-surface-high hover:text-ink md:self-auto"
              >
                <Icon name="lock" size={15} />
                Disconnect
              </button>
            )}
          </div>
          <div className="mt-4 flex items-start gap-2 rounded-xl border border-warn/30 bg-warn/10 p-3 text-xs leading-5 text-ink">
            <Icon name="warning" size={16} className="mt-0.5 shrink-0 text-warn" />
            <p>
              The admin key is kept only in this page’s memory and cleared when you disconnect or reload. It is still present in the browser while connected. Use this page only on a trusted device and connection.
            </p>
          </div>

          {!connected && (
            <form onSubmit={handleConnect} className="mt-5 flex flex-col gap-3 sm:flex-row">
              <label className="sr-only" htmlFor="admin-api-key">ADMIN_API_KEY</label>
              <input
                id="admin-api-key"
                type="password"
                autoComplete="off"
                value={key}
                onChange={(event) => setKey(event.target.value)}
                placeholder="Enter ADMIN_API_KEY"
                className="min-w-0 flex-1 rounded-lg border border-line bg-bg px-3 py-2.5 text-sm text-ink placeholder:text-muted"
              />
              <button
                type="submit"
                disabled={busy === 'connect'}
                className="flex items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-wait disabled:opacity-60"
              >
                <Icon name={busy === 'connect' ? 'progress_activity' : 'key'} size={17} spin={busy === 'connect'} />
                {busy === 'connect' ? 'Connecting…' : 'Connect'}
              </button>
            </form>
          )}
          {connected && (
            <div role="status" className="mt-4 flex items-center gap-2 text-sm text-ok">
              <Icon name="check_circle" size={17} />
              Connected to the protected admin API.
            </div>
          )}
        </section>

        {(error || notice) && (
          <div
            role={error ? 'alert' : 'status'}
            className={`flex items-start gap-2 rounded-xl border p-3 text-sm ${
              error ? 'border-bad/30 bg-bad/10 text-ink' : 'border-ok/30 bg-ok/10 text-ink'
            }`}
          >
            <Icon name={error ? 'error' : 'check_circle'} size={17} className={error ? 'text-bad' : 'text-ok'} />
            <span className="min-w-0 break-words">{error || notice}</span>
            <button type="button" onClick={() => { setError(''); setNotice('') }} aria-label="Dismiss message" className="ml-auto text-muted hover:text-ink">
              <Icon name="close" size={16} />
            </button>
          </div>
        )}

        {connected && (
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.7fr)_minmax(280px,0.8fr)]">
            <section className="min-w-0 rounded-2xl border border-line bg-surface p-5 md:p-6">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-semibold">Rulebook draft</h2>
                  <p className="mt-1 text-xs text-muted">
                    {fileName || 'Choose a Markdown rulebook or export the active one.'}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={handleExport}
                    disabled={Boolean(busy)}
                    className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-medium hover:bg-surface-high disabled:opacity-50"
                  >
                    <Icon name="download" size={15} />
                    Export current
                  </button>
                  <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-medium hover:bg-surface-high">
                    <Icon name="upload_file" size={15} />
                    Open Markdown
                    <input type="file" accept=".md,text/markdown,text/plain" onChange={handleFile} className="sr-only" />
                  </label>
                </div>
              </div>
              <textarea
                value={markdown}
                onChange={(event) => {
                  setMarkdown(event.target.value)
                  setSelectedDraft(null)
                }}
                disabled={!connected}
                spellCheck="false"
                aria-label="Business rules Markdown editor"
                placeholder="Export current rules or open a Markdown file to begin."
                className="mt-4 min-h-[360px] w-full resize-y rounded-xl border border-line bg-bg p-4 font-mono text-xs leading-5 text-ink placeholder:font-sans placeholder:text-muted"
              />
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <p className="max-w-md text-xs leading-5 text-muted">
                  If <code className="rounded bg-bg px-1 py-0.5">_meta.version</code> matches the active rulebook, it is incremented automatically. Draft creation validates and previews only; it does not change active rules.
                </p>
                <button
                  type="button"
                  onClick={handleCreateDraft}
                  disabled={!markdown.trim() || Boolean(busy)}
                  className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Icon name={busy === 'draft' ? 'progress_activity' : 'fact_check'} size={17} spin={busy === 'draft'} />
                  {busy === 'draft' ? 'Validating…' : 'Create draft & preview'}
                </button>
              </div>
            </section>

            <aside className="space-y-5">
              {selectedDraft && (
                <section className="rounded-2xl border border-line bg-surface p-5">
                  <div className="flex items-center justify-between gap-3">
                    <h2 className="font-semibold">Preview</h2>
                    <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${
                      selectedDraft.status === 'published' ? 'bg-ok/15 text-ok' : 'bg-warn/15 text-warn'
                    }`}>
                      {selectedDraft.status}
                    </span>
                  </div>
                  <p className="mt-2 break-all font-mono text-[11px] text-muted">{selectedDraft.revision_id}</p>
                  <div className="mt-4 grid grid-cols-2 gap-3">
                    <div className="rounded-lg bg-bg p-3">
                      <p className="text-[11px] text-muted">Version</p>
                      <p className="mt-1 break-all text-sm font-semibold">{selectedDraft.preview.rulebook_version}</p>
                    </div>
                    <div className="rounded-lg bg-bg p-3">
                      <p className="text-[11px] text-muted">Terms</p>
                      <p className="mt-1 text-sm font-semibold">{selectedDraft.preview.term_count}</p>
                    </div>
                    <div className="col-span-2 rounded-lg bg-bg p-3">
                      <p className="text-[11px] text-muted">Ontology entities</p>
                      <p className="mt-1 text-sm font-semibold">{selectedDraft.preview.ontology_entity_count}</p>
                    </div>
                  </div>
                  <div className="mt-4 space-y-4">
                    <ChangeGroup title="Rule terms" changes={selectedDraft.preview.changes?.terms} />
                    <ChangeGroup title="Ontology entities" changes={selectedDraft.preview.changes?.ontology_entities} />
                  </div>
                  {selectedDraft.status === 'draft' && (
                    <button
                      type="button"
                      onClick={handlePublish}
                      disabled={Boolean(busy)}
                      className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-ok px-4 py-2.5 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-wait disabled:opacity-50"
                    >
                      <Icon name={busy === 'publish' ? 'progress_activity' : 'publish'} size={17} spin={busy === 'publish'} />
                      {busy === 'publish' ? 'Publishing…' : 'Publish this draft'}
                    </button>
                  )}
                  {selectedDraft.status === 'draft' && (
                    <p className="mt-2 text-center text-[11px] leading-5 text-muted">
                      Publishing makes this rulebook active for new chat requests.
                    </p>
                  )}
                </section>
              )}

              <section className="rounded-2xl border border-line bg-surface p-5">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">Revision history</h2>
                    <p className="mt-1 text-xs text-muted">Select a draft to inspect its preview.</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => refreshRevisions(key).catch((refreshError) => setError(refreshError.message))}
                    disabled={Boolean(busy)}
                    aria-label="Refresh revision history"
                    className="rounded-lg border border-line p-2 text-muted hover:bg-surface-high hover:text-ink disabled:opacity-50"
                  >
                    <Icon name="refresh" size={16} />
                  </button>
                </div>
                <div className="mt-4 max-h-80 space-y-2 overflow-y-auto">
                  {revisions.length === 0 && (
                    <p className="rounded-lg bg-bg p-3 text-xs text-muted">No saved revisions yet.</p>
                  )}
                  {revisions.map((revision) => (
                    <button
                      key={revision.revision_id}
                      type="button"
                      onClick={() => handleSelectRevision(revision)}
                      disabled={Boolean(busy) || revision.status !== 'draft'}
                      className={`w-full rounded-lg border border-line bg-bg p-3 text-left transition-colors ${
                        revision.status === 'draft' ? 'hover:border-primary/50 hover:bg-surface-high' : 'cursor-default'
                      }`}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[11px] text-ink">{revision.revision_id.slice(0, 8)}…</span>
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                          revision.status === 'published' ? 'bg-ok/15 text-ok' :
                            revision.status === 'draft' ? 'bg-warn/15 text-warn' : 'bg-surface-high text-muted'
                        }`}>{revision.status}</span>
                      </span>
                      <span className="mt-1 block text-[11px] text-muted">{formatDate(revision.created_at)}</span>
                    </button>
                  ))}
                </div>
              </section>
            </aside>
          </div>
        )}
      </main>
    </div>
  )
}
