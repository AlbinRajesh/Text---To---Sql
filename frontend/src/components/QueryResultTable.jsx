import Icon from './icon.jsx'
import {
  columnLabel,
  copyText,
  downloadCSV,
  formatCell,
  getColumns,
  isIdentifierColumn,
  replaceUniqueText,
  toCSV,
} from '../utils/csv.js'

/** Shows a result as a table, or as a big number when it is 1 row x 1 column. */
export default function QueryResultTable({ response, notify, question, onUseSuggestion }) {
  const rows = response.results || []
  const columns = getColumns(response)
  const labels = response.column_labels || {}
  const columnFormats = response.column_formats || {}
  const rowCount = response.row_count ?? rows.length

  if (rows.length === 0) {
    return (
      <div className="rounded-2xl border border-line bg-surface p-5 text-sm text-muted" role="status">
        <p>{response.message || 'No matching records found.'}</p>
        {response.suggestions?.length > 0 && (
          <div className="mt-4 space-y-2">
            <p className="font-medium text-ink">Did you mean?</p>
            {response.suggestions.map((suggestion, index) => {
              const revisedQuestion = replaceUniqueText(
                question,
                suggestion.original,
                suggestion.suggested,
              )
              return (
                <div key={`${suggestion.column}-${suggestion.original}-${index}`} className="flex flex-wrap items-center gap-2">
                  <span>
                    {suggestion.original} <span aria-hidden="true">→</span> <strong className="text-ink">{suggestion.suggested}</strong>
                    <span className="ml-1">({columnLabel(suggestion.column, labels)})</span>
                  </span>
                  {revisedQuestion && (
                    <button
                      type="button"
                      onClick={() => onUseSuggestion(question, suggestion, revisedQuestion)}
                      className="rounded-lg border border-line bg-surface-high px-2.5 py-1 text-xs font-medium text-ink hover:border-primary/50"
                    >
                      Run suggestion
                    </button>
                  )}
                </div>
              )
            })}
            <p className="text-xs">
              Selecting a suggestion submits the corrected question immediately. When a saved fast path is available, it runs the validated query directly; otherwise it uses the normal query flow.
            </p>
          </div>
        )}
      </div>
    )
  }

  if (response.answer_type === 'single_value' || (rows.length === 1 && columns.length === 1)) {
    const col = columns[0]
    const value = response.answer_type === 'single_value'
      ? response.single_value
      : rows[0]?.[col]
    return (
      <div className="w-full rounded-2xl border border-line bg-surface p-6 shadow-lg">
        <div className="font-mono text-5xl font-bold tracking-tight text-ink">
          {formatCell(value, columnFormats[col] || (isIdentifierColumn(col) ? 'identifier' : 'number'))}
        </div>
        <div className="mt-2 text-sm font-medium text-muted">{columnLabel(col, labels)}</div>
        {response.message && (
          <p className="mt-3 text-xs text-muted" role="status">{response.message}</p>
        )}
      </div>
    )
  }

  const onCopy = async () => notify((await copyText(toCSV(columns, rows, labels))) ? 'Copied' : 'Copy failed')

  return (
    <div className="flex w-full flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-lg">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-surface-high/60 p-3">
        <span className="text-sm font-semibold text-ink">
          {rowCount} {rowCount === 1 ? 'row' : 'rows'}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onCopy}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink transition-colors hover:bg-surface-high"
          >
            <Icon name="content_copy" size={15} className="text-accent" />
            Copy as CSV
          </button>
          <button
            type="button"
            onClick={() => downloadCSV(columns, rows, 'results.csv', labels)}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink transition-colors hover:bg-surface-high"
          >
            <Icon name="download" size={15} className="text-primary" />
            Download CSV
          </button>
        </div>
      </div>

      <div className="max-h-96 w-full overflow-auto">
        <table className="w-full text-left text-xs">
          <thead className="sticky top-0 z-10 bg-surface-high text-muted">
            <tr>
              {columns.map((c) => (
                <th key={c} scope="col" className="whitespace-nowrap px-4 py-3 font-medium">
                  {columnLabel(c, labels)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className={`${i % 2 ? 'bg-surface-high/40' : ''} transition-colors hover:bg-surface-high`}>
                {columns.map((c) => {
                  const format = columnFormats[c] || (isIdentifierColumn(c) ? 'identifier' : 'number')
                  const isNum = typeof row[c] === 'number' && format !== 'identifier'
                  return (
                    <td key={c} className={`px-4 py-3 text-ink ${isNum ? 'text-right font-mono' : ''}`}>
                      {formatCell(row[c], format)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {response.truncated && (
        <div className="border-t border-warn/30 bg-warn/10 px-4 py-3 text-xs text-ink" role="status">
          Showing the first {rowCount} rows. Refine your question to narrow the results.
        </div>
      )}
      {response.message && (
        <div className="border-t border-warn/30 bg-warn/10 px-4 py-3 text-xs text-ink" role="status">
          {response.message}
        </div>
      )}
    </div>
  )
}