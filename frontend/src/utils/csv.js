/** "total_plans" -> "Total plans" */
export function humanize(column) {
  const s = String(column).replace(/_/g, ' ').replace(/\s+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function columnLabel(column, labels = {}) {
  return labels?.[column] || humanize(column)
}

export function getColumns(response) {
  if (response?.columns?.length) return response.columns
  const first = response?.results?.[0]
  return first ? Object.keys(first) : []
}

export function isIdentifierColumn(column) {
  const parts = String(column).toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)
  return parts.some((part) => [
    'code', 'guid', 'identifier', 'id', 'ids', 'mobile', 'phone', 'pincode', 'postcode', 'uuid', 'zipcode',
  ].includes(part))
}

export function formatCell(value, format = 'number', column = '') {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    if (format === 'identifier') return String(value)
    const key = String(column).toLowerCase()
    const rating = /(^|_)(average|avg)_?rating($|_)/.test(key)
    const percentage = /percent|percentage|(^|_)pct($|_)/.test(key)
    const formatted = value.toLocaleString(undefined, {
      minimumFractionDigits: rating ? 2 : 0,
      maximumFractionDigits: rating ? 2 : percentage ? 3 : 4,
    })
    return percentage ? `${formatted}%` : formatted
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return String(value)
}

export function replaceUniqueText(source, target, replacement) {
  if (!source || !target) return null
  const escaped = target.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const matches = [...source.matchAll(new RegExp(escaped, 'giu'))]
  if (matches.length !== 1) return null
  const { index } = matches[0]
  return `${source.slice(0, index)}${replacement}${source.slice(index + target.length)}`
}

// Escape one CSV cell. Also blocks spreadsheet formula injection (= + - @).
function escapeCell(value) {
  if (value === null || value === undefined) return ''
  let s = String(value)
  if (typeof value !== 'number' && /^[=+\-@]/.test(s)) s = `'${s}`
  if (/[",\r\n]/.test(s)) s = `"${s.replace(/"/g, '""')}"`
  return s
}

export function toCSV(columns, rows, labels = {}) {
  const header = columns.map((c) => escapeCell(columnLabel(c, labels))).join(',')
  const body = rows.map((r) => columns.map((c) => escapeCell(r[c])).join(','))
  return [header, ...body].join('\r\n')
}

export function downloadCSV(columns, rows, filename = 'results.csv', labels = {}) {
  const blob = new Blob(['\uFEFF' + toCSV(columns, rows, labels)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
