import { formatCell, getColumns } from './csv.js'

const normalized = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '_')

function findColumn(columns, pattern) {
  return columns.find((column) => pattern.test(normalized(column)))
}

function displayValue(value, column) {
  return formatCell(value, undefined, column)
}

function isNumericValue(value) {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
}

function summaryForRows(question, response) {
  const columns = getColumns(response)
  const rows = response.results || []
  const q = question.toLowerCase()
  const countColumn = findColumn(
    columns,
    /(^|_)(booking|review|shop|invoice|offer)_?(count|number)($|_)|(^|_)total_?(count|number)($|_)/,
  )
  const revenueColumn = findColumn(columns, /(^|_)(total_)?(revenue|amount|collected|final_amount)($|_)/)
  const averageRatingColumn = findColumn(columns, /(^|_)(average|avg)_?rating($|_)/)
  const reviewCountColumn = findColumn(columns, /(^|_)(total_)?reviews?(_count)?($|_)/)
  const fiveStarColumn = findColumn(columns, /five_?star.*(percent|percentage)|percent.*five_?star/)
  const monthColumn = findColumn(columns, /(^|_)month($|_)/)
  const categoryColumn = findColumn(columns, /category.*name/) || findColumn(columns, /(^|_)category($|_)|category/)
  const offerNameColumn = findColumn(columns, /(^|_)(offer_)?(heading|name|title)($|_)/)
  const discountColumn = findColumn(columns, /discount.*(percent|percentage)|(^|_)(percent|percentage)_?discount/)
  const shopNameColumn = findColumn(columns, /(^|_)(shop_)?name($|_)/)
  const year = q.match(/\b(20\d{2})\b/)?.[1]

  if (
    rows.length > 1 &&
    monthColumn &&
    countColumn &&
    /booking/.test(q) &&
    rows.every((row) => isNumericValue(row[countColumn]))
  ) {
    const peakCount = Math.max(...rows.map((row) => Number(row[countColumn])))
    const peakMonths = rows
      .filter((row) => Number(row[countColumn]) === peakCount)
      .map((row) => String(row[monthColumn]))
    if (!peakMonths.length) return null
    const period = year ? ` in ${year}` : ''
    const leaderLabel = peakMonths.length === 1 ? 'had' : 'tied for'
    return `${peakMonths.join(' and ')} ${leaderLabel} the most bookings${period}, with ${displayValue(peakCount, countColumn)} each.`
  }

  if (
    rows.length === 1 &&
    averageRatingColumn &&
    reviewCountColumn &&
    fiveStarColumn &&
    isNumericValue(rows[0][averageRatingColumn]) &&
    isNumericValue(rows[0][reviewCountColumn]) &&
    isNumericValue(rows[0][fiveStarColumn])
  ) {
    const row = rows[0]
    return `Across ${displayValue(row[reviewCountColumn], reviewCountColumn)} reviews, the average rating is ${formatCell(row[averageRatingColumn], undefined, 'average_rating')}, and ${displayValue(row[fiveStarColumn], fiveStarColumn)} are five-star.`
  }

  if (
    rows.length === 1 &&
    countColumn &&
    revenueColumn &&
    isNumericValue(rows[0][countColumn]) &&
    isNumericValue(rows[0][revenueColumn]) &&
    /paid invoice/.test(q)
  ) {
    const row = rows[0]
    return `There are ${displayValue(row[countColumn], countColumn)} paid invoices, with ${displayValue(row[revenueColumn], revenueColumn)} collected in total.`
  }

  if (
    rows.length > 0 &&
    categoryColumn &&
    findColumn(columns, /shop_?count|(^|_)shops?($|_)/) &&
    /categor/.test(q) &&
    /shop/.test(q) &&
    rows.every((row) =>
      typeof row[categoryColumn] === 'string' &&
      isNumericValue(row[findColumn(columns, /shop_?count|(^|_)shops?($|_)/)]),
    )
  ) {
    const shopsColumn = findColumn(columns, /shop_?count|(^|_)shops?($|_)/)
    const active = /active/.test(q) ? ' active' : ''
    const sortedRows = [...rows].sort((left, right) => Number(right[shopsColumn]) - Number(left[shopsColumn]))
    const leaders = sortedRows.filter((row) => Number(row[shopsColumn]) === Number(sortedRows[0][shopsColumn]))
    const leaderNames = leaders.map((row) => String(row[categoryColumn]))
    const leader = sortedRows[0]
    const runnerUp = sortedRows[leaders.length]
    const lead = leaders.length > 1
      ? `${leaderNames.join(' and ')} are tied for the most${active} shops (${displayValue(leader[shopsColumn], shopsColumn)}).`
      : `${leaderNames[0]} has the most${active} shops (${displayValue(leader[shopsColumn], shopsColumn)}).`
    const next = runnerUp
      ? ` Next is ${String(runnerUp[categoryColumn])} (${displayValue(runnerUp[shopsColumn], shopsColumn)}).`
      : ''
    return `${lead}${next}`
  }

  if (
    rows.length > 0 &&
    offerNameColumn &&
    discountColumn &&
    /discount/.test(q) &&
    rows.every((row) =>
      typeof row[offerNameColumn] === 'string' && isNumericValue(row[discountColumn]),
    )
  ) {
    const leader = rows.reduce((best, row) =>
      Number(row[discountColumn]) > Number(best[discountColumn]) ? row : best,
    )
    return `${rows.length} offers meet the requested discount threshold. The largest listed discount is ${displayValue(leader[discountColumn], discountColumn)} for "${String(leader[offerNameColumn])}".`
  }

  if (
    rows.length > 0 &&
    shopNameColumn &&
    countColumn &&
    /review/.test(q) &&
    /shop/.test(q) &&
    typeof rows[0][shopNameColumn] === 'string' &&
    isNumericValue(rows[0][countColumn])
  ) {
    const mostReviews = Math.max(...rows.map((row) => Number(row[countColumn])))
    const leaders = rows
      .filter((row) => Number(row[countColumn]) === mostReviews)
      .map((row) => String(row[shopNameColumn]))
    const leaderText = leaders.length > 1
      ? `${leaders.join(' and ')} are tied with ${displayValue(mostReviews, countColumn)} reviews each.`
      : `${leaders[0]} leads with ${displayValue(mostReviews, countColumn)} reviews.`
    return `${rows.length} shops match your criteria. ${leaderText}`
  }

  if (
    rows.length === 1 &&
    categoryColumn &&
    revenueColumn &&
    /categor/.test(q) &&
    /booking|revenue/.test(q) &&
    typeof rows[0][categoryColumn] === 'string' &&
    isNumericValue(rows[0][revenueColumn])
  ) {
    const row = rows[0]
    return `${String(row[categoryColumn])} generated the most booking revenue: ${displayValue(row[revenueColumn], revenueColumn)}.`
  }

  if (
    rows.length === 1 &&
    countColumn &&
    isNumericValue(rows[0][countColumn]) &&
    /never.*offer|offer.*never/.test(q)
  ) {
    return `${displayValue(rows[0][countColumn], countColumn)} active shops have never posted an offer.`
  }

  return null
}

export function answerSummary(question, response) {
  const summary = summaryForRows(question, response)
  if (summary) return summary
  const existing = response?.summary
  if (!existing || /^returned \d+ rows?\.?$/i.test(existing)) return null
  return existing
}

export function followUpQuestions(question, response) {
  if (response?.status && response.status !== 'ok') return []
  if (response?.truncated || !response?.results?.length) return []

  const columns = getColumns(response).map(normalized)
  const q = question.toLowerCase()
  const has = (pattern) => columns.some((column) => pattern.test(column))

  if (has(/month/) && has(/booking/) && /\b20\d{2}\b/.test(q)) {
    const year = q.match(/\b(20\d{2})\b/)[0]
    return [`Show total booking revenue for ${year}`, `Compare bookings in ${year} with the previous year`]
  }
  if (has(/rating/) && has(/review/)) {
    return ['Show the review count by rating', 'Show shops with the most reviews']
  }
  if (has(/discount/) && has(/offer/)) {
    return ['Show the 5 offers with the highest discounts', 'Show offers with no discount']
  }
  if (has(/category/) && has(/shop/)) {
    return ['Show the top 10 categories by active shop count', 'Show the categories with the fewest active shops']
  }
  if (has(/invoice/) && has(/amount|revenue|collected/)) {
    return ['Show paid invoice totals by month', 'Break down paid invoices by subscription plan']
  }
  if (has(/review/) && has(/shop/)) {
    return ['Show the average rating for these shops', 'Show the review count by rating']
  }
  return []
}
