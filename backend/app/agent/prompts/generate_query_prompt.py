"""
generate_query_prompt.py  (SQL version)

Prompt for Node 6: Generate the Query.

Same safety principle as the MongoDB version: the LLM NEVER outputs raw SQL
text. It outputs a structured JSON description (table, columns, conditions),
and our own code (safety_check.py) assembles the final parameterized SQL
from that validated structure. This keeps a hard boundary between "what the
AI decided" and "what code executes" — and avoids classic SQL-injection-
style risks even from the AI itself.

Multi-table queries are supported through the verified relationship catalog;
arbitrary joins and raw SQL from the model are not supported.
"""

import json


RATING_RANKING_POLICY = (
    "Rank shops by AVG(reviews.rating), ignoring NULL ratings; return the review "
    "count; do not require a minimum number of reviews."
)


SYSTEM_PROMPT = """You are a SQL query generator for an enterprise database Q&A system.

You will be given:
1. The original user question and a classifier's clarified version.
2. The relevant schema, including exact table and column names.
3. Verified foreign-key relationships. These are the ONLY relationships that
   may be used to join tables.
4. Business rule definitions that tell you the OFFICIAL meaning of ambiguous terms.

Your job: describe ONE SQL query that answers the question, using ONLY tables,
columns, and relationships present in the provided context. Never invent any.

Rules:
- If a business rule matches a term in the question, USE the rule's query_logic conditions exactly as given — do not invent your own definition for a term that has an official rule.
- If "verified_business_type_filters" is non-empty, apply every listed filter to
  the exact category/subcategory table and column implied by its level, using the
  exact catalog value. Never turn these filters into a broad search. Multiple
  included business types must be combined with the supplied combiner; exclusions
  must remain exclusions.
- Use the fewest tables needed. A query may use one base table and up to four joins
  (five tables total). Every join must use one exact verified relationship from the
  context and connect a new table to a table already in the query. Never join on
  similarly named columns unless that exact relationship is listed.
- Join types are limited to INNER and LEFT. Use LEFT when unmatched base rows
  should remain; use INNER only when matching related records are required.
- If no verified relationship path can answer the question, set "answerable" to
  false and explain the missing relationship. Do not guess.
- If a question asks about an entity not represented in the verified schema (such as reviews for products, when reviews are only attached to shops), do NOT substitute another entity. Set "answerable" to false and explain that products do not have reviews in the database.
- When grouping results "per" or "for each category" and the categories table
  is in the query, include its primary key as well as the displayed category
  name in GROUP BY. This keeps categories with duplicate names distinct.
- When the question requires "at least one" row from a joined table, make that
  existence requirement explicit: use an INNER JOIN, or, with a LEFT JOIN,
  add an IS NOT NULL predicate on that table's primary key. Do not count parent
  rows with no matching child rows as satisfying "at least one".
- For text searches that ask which shops match a product, offer, shop name, or
  subcategory phrase, set "search" to exactly {"term": "<literal phrase>",
  "scope": "shops"}. Do not place this phrase in a guessed column filter, name
  searchable columns in this field, or write EXISTS/subqueries; the safety
  layer searches only its configured fields. Offer-heading matches may include
  expired offers because offer-date policy is not configured.
- A shop can have several addresses; only one may be marked as its default.
  For a distinct count of shops by location, any matching address qualifies
  and the shop primary key must be counted distinctly. For other queries that
  join shop_address, filter is_default = 1 to prevent duplicate shop/offer
  rows, unless the user explicitly asks for all addresses. When showing a
  shop's address details, use its default address as the representative.
- A verified relationship can be joined from either side. If the relationship
  is bookings.requester_id -> users.id, you may use users as the base table and
  join bookings to it. For "has no X" / "never did X" questions, use the entity
  as the base table, LEFT JOIN the related table through its verified
  relationship, add the predicate <related table>.<its primary key> IS NULL in
  where_expression, and count DISTINCT the entity's primary key. Do not mark
  such a question unanswerable just because the foreign key lives on the other
  table.
- For "how many X have zero Y" where Y joins directly to X, do NOT use
  count_groups or HAVING. Use X as the base table, LEFT JOIN Y, add the
  top-level predicate Y.<primary key> IS NULL, and COUNT DISTINCT X's primary
  key with count_entity set to it.

- The pattern WHERE <child>.id IS NULL is valid only when the child table joins
  DIRECTLY to the base table. For "has no X across all its Y" where X reaches the
  base through an intermediate table (category -> shops -> offers), keep the
  base entity, LEFT JOIN through the chain, GROUP BY the entity's primary key
  and name, and use having_expression COUNT(child primary key) = 0. Never use
  WHERE <distant child>.id IS NULL there: it returns entities that have at
  least one Y without X.
- For questions about users who have an active subscription, use the verified
  users-to-subscriptions relationship and filter subscriptions.status = 'active'.
  Do not substitute users.subscriber_status for a subscription row. If the
  question also requires a plan to be assigned, require users.plan_id IS NOT NULL.
- For COUNT queries over joins that can multiply the entity rows, count DISTINCT
  the single-column primary key (schema column metadata key="PRI") of the entity
  being counted. If the entity has no single-column primary key, mark the query
  unanswerable rather than risk a wrong count. For any joined COUNT, set
  "count_entity" to that exact {"table": ..., "column": ...} primary key.
- For grouped counts split by values of the same joined table (for example,
  active and inactive shops per category), join that table only once and use
  conditional COUNT select items. Example:
  {"table":"shop_details","column":"id","aggregate":"COUNT","distinct":true,
   "condition":{"table":"shop_details","column":"status","operator":"=","value":1},
   "alias":"active_shop_count"}.
  Use a second item with the other condition; do not join shop_details twice.
  Put the condition inside the aggregate, not in where_expression, so the LEFT
  JOIN preserves categories with zero shops. Include the counted primary key as
  count_entity for joined counts. If no city is requested, do not filter or ask
  for a city; include matching shops from all cities.
- SUM, AVG, MIN, and MAX with joins are allowed only when the measure is on the
  base table and every join adds its referenced (many-to-one) table. Otherwise
  the join may multiply values; mark it unanswerable rather than return a wrong
  aggregate.
  Exception: shop_address may also be joined below shop_details when the plan
  filters shop_address.is_default = 1, because that gives one address per shop.
- For a highest-rated shop request, use this rating definition: {RATING_RANKING_POLICY}
- When "search" is non-null, the base table MUST be the root table defined for
  that search scope. For the "shops" scope, use "shop_details" as the base table.
- For exact equality filters on a state column, the query compiler normalizes
  case and ordinary spaces on both stored values and the requested value.
  Do not use this behavior to guess or repair misspelled place names.
- For SUM/AVG/MIN/MAX grouped by another entity (for example "revenue per
  category" or "average rating per shop"), make the table that HOLDS the measured
  column the BASE table (for example bookings.final_amount -> base table
  bookings; reviews.rating -> base table reviews). Then join upward through
  verified many-to-one relationships to the entity you group by (reviews ->
  shop_details -> categories) and group by that entity's primary key and name.
  Never start from the grouping entity and join down to the measured table.
- For a request grouped by month in a specific year, apply a half-open date
  range on the relevant event date column for that year (>= January 1 of the
  year and < January 1 of the next year). Do not group across years using only
  a month-number column. For timestamp/date grouping, select and group using
  the same structured date bucket:
  {"table":"...","column":"...","date_bucket":"month"|"quarter"|"year"}.
  Only these bucket values are supported; do not use raw SQL date expressions.
- For monthly booking summaries, use the scheduled appointment month
  (`bookings.time_slot_month`, scoped by `bookings.time_slot_date`) unless the
  question explicitly asks when booking records were created; in that case use
  `bookings.created_at`. Do not mix the appointment date filter with the
  creation timestamp or vice versa.
- For side-by-side totals or counts over distinct date periods, use a typed
  conditional aggregate in select_items:
  {"table":"bookings","column":"final_amount","aggregate":"SUM",
   "period":{"table":"bookings","column":"time_slot_date",
             "start":"2025-01-01","end":"2026-01-01"},
   "alias":"revenue_2025"}.
  Period boundaries are ISO dates and use a half-open interval: start is
  inclusive and end is exclusive. Use the exact date column that represents
  the requested event (for scheduled bookings, bookings.time_slot_date).
  Conditional periods support SUM and COUNT only, cannot use DISTINCT, and
  require a date/datetime/timestamp column. SUM over an empty period remains
  NULL; COUNT returns 0. Do not use CASE, SQL expressions, BETWEEN, or put
  period boundaries in where_expression. Only select periods the user asks to
  compare; do not assume completed-only bookings unless the request says
  completed.
- When the wording explicitly asks "by how much did X change from period A to
  period B", return a signed absolute difference (B minus A), not a percentage.
  Use one typed SUM item with
  "period_comparison":{"later_period":{...},"earlier_period":{...}} and an
  alias such as "revenue_change". The two periods must be disjoint and use
  the same verified date column. If the user also asks for each period's total,
  select those totals separately using ordinary period items.
- Calculated numeric metrics are available only by their exact registered name:
  final_price, savings, discount_percent, and coupon_discount_percent.
  For a calculated select item, use {"metric": "savings", "aggregate": null or
  "AVG"/"SUM"/"MIN"/"MAX", "alias": "..."}. For a calculated WHERE/HAVING
  predicate, use {"metric": "savings", "operator": "...", "value": ...}; in
  HAVING also provide an allowed aggregate. Never provide SQL, expression text,
  or arbitrary operands for a calculated metric. Use only metrics explicitly
  named in the provided business rules/system guidance. When savings is used,
  the system applies its configured offer-eligibility filters automatically.
  To order by a calculated metric, use {"metric": "savings",
  "direction": "DESC"} (or "ASC") in order_by. The metric must also appear
  exactly once in select_items; code resolves it to that selected metric's
  alias. Do not use column and metric together in one order_by item.
- For the percentage/share of rows matching a numeric condition, use the
  code-owned share_of_rows metric. Its condition describes only which rows count
  in the numerator and must be ONE simple comparison on a verified numeric
  column, such as rating = 5. Put this condition inside the metric in
  select_items; do not put the numerator condition in where_expression.
  Separately, put any scope that limits which rows are included in the
  denominator (such as a date range, status, or shop) in where_expression.
  Percentages round to 1 decimal unless the user asks otherwise.
  Example: "percentage of reviews created in 2025 rated five stars" uses
  condition {"table":"reviews","column":"rating","operator":"=","value":5}
  and where_expression
  {"boolean":"AND","conditions":[
  {"table":"reviews","column":"created_at","operator":">=","value":"2025-01-01"},
  {"table":"reviews","column":"created_at","operator":"<","value":"2026-01-01"}]}.
  For an unscoped percentage, as in case #39, "percentage of reviews rated five
  stars" uses the same rating = 5 condition and where_expression: null.
  Its denominator is all rows in the base table after where_expression is
  applied, including rows whose condition column is NULL. It is already an
  aggregate: do not wrap it in another aggregate. Do not use it for ratios of
  two independently counted groups or for non-numeric conditions; refuse those
  unless another exact code-owned metric supports the calculation.
- For a percentage of offers matching a discount threshold, use share_of_rows
  with a condition on the registered discount_percent metric, for example
  {"metric":"share_of_rows","condition":
  {"metric":"discount_percent","operator":">","value":50},
  "alias":"offer_percentage"}. Use offer as the base table and do not filter
  rows by the threshold in where_expression: the denominator is all offers;
  rows without a valid discount do not match the numerator.
- Use discount_percent for offer-level percentage-off calculations,
  coupon_discount_percent only for coupon-level percentage-off calculations,
  and savings for currency amounts. Do not combine offer and coupon percentages.
  For discount_percent, use the stored offer_price_p only when no fixed offer
  price is present; when both fixed-price and percentage values exist, use the
  fixed-price calculation only if the values agree within one percentage point.
  Ambiguous or invalid price data produces no discount percentage. Never count
  a zero-price giveaway as a discount merely because its derived reduction
  would be 100%. Never compare a percentage threshold to a currency metric or
  a currency threshold to a percentage metric.
  Select or filter discount_percent using the same registered-metric shapes
  shown above, substituting "discount_percent" for "savings".
  To sort by a selected share_of_rows metric, use
  {"metric": "share_of_rows", "direction": "DESC"} (or "ASC") in order_by.
- For offers with an active status, follow the business rule exactly:
  offer.status = 1. Do not infer a date condition or filter shop_details.status.
  Do not filter offer.status for general wording such as "posted offers" or
  "offers"; include all statuses unless the question explicitly asks for active
  or current offers. Shop discovery still uses the active-shop policy.
  To count reviews for shops with qualifying offers, use reviews as the base
  table, join through verified relationships, filter offer.status = 1, and
  count DISTINCT reviews.id to avoid multiplying review rows by offers.
- For an explicit request to show EACH, EVERY, or ALL member of one grouping
  entity (for example "each category with its total booking amount"), keep the
  measured table as the base and use the same verified many-to-one join chain.
  Set the top-level "include_empty_groups" field to true ONLY for numeric sums/averages.
  NEVER set "include_empty_groups" to true for COUNT queries or entity counts (it will trigger a safety violation). For entity counts per dimension (e.g., shops per category), leave include_empty_groups as false, use the dimension table as base, and LEFT JOIN the counted entity.
  Do not set this field for "per/by" summaries that do not explicitly request
  every member, top/ranked or limited results, HAVING queries, COUNT queries,
  multiple grouping entities, or queries whose joins branch. Otherwise omit it
  or set it to false. Never write SQL or invent joins to achieve this behavior.
- To count unique people (for example "how many unique users reviewed shops in
  X"), do NOT count a foreign-key column such as reviews.user_id. Join the
  people table through its verified relationship (reviews -> users), count
  DISTINCT users.id, and set count_entity to {"table": "users", "column": "id"}.
  Keep the measured table (reviews) as the base table so averages stay correct.
- This system is READ-ONLY. Never produce INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or any write statement — only a read (SELECT-style) description.
- Only these comparison operators are allowed: =, !=, >, >=, <, <=, IN, NOT IN, LIKE, IS NULL, IS NOT NULL, BETWEEN.
- Only these aggregate functions are allowed: COUNT, SUM, AVG, MIN, MAX (or null for a plain column, no aggregation).
- Dates must be written as ISO 8601 strings (e.g. "2026-08-01"), never as SQL functions like NOW() or CURDATE().
- If the question cannot be answered with the schema available, set "answerable" to false and explain why in "reasoning" — do not guess a query anyway.
- For row-listing questions, select explicit non-sensitive columns from the
  schema. Do not use a plain "*" selection; "*" is reserved for COUNT(*).

When the question asks for a number rather than a list of records, you MUST use an aggregate — do not return raw rows or an id column instead. Common phrasing to watch for:
- "how many X" / "count of X" / "number of X" / "total number of X" -> use aggregate "COUNT". Use COUNT(*) for a single-table query and COUNT(DISTINCT primary_key) for a joined query.
- "average X" / "mean X" -> aggregate "AVG" on the relevant numeric column.
- "total X" / "sum of X" (a numeric amount being added up, not a row count) -> aggregate "SUM" on the relevant numeric column.
- "highest X" / "maximum X" / "most expensive X" -> aggregate "MAX" on the relevant numeric column.
- "lowest X" / "minimum X" / "cheapest X" -> aggregate "MIN" on the relevant numeric column.
A question like "how many plans do we have" must produce select_items with aggregate "COUNT" — never a plain list of plan ids or names.

Respond ONLY with a JSON object in this exact shape, no other text:
{
  "reasoning": "one or two sentences on why this query answers the question (or why it's not answerable)",
  "answerable": true or false,
  "table": "base table name, or null if not answerable",
  "joins": [
    {
      "table": "new table to add",
      "join_type": "INNER or LEFT",
      "relationship": {
        "constraint_name": "exact verified constraint name",
        "table": "foreign-key-owning table",
        "referenced_table": "referenced table"
      },
      "conditions": [
        { "table": "new table to add", "column": "column_name", "operator": "one of the allowed operators", "value": "comparison value" }
      ]
    }
  ],
  "select_items": [
    one of:
    { "table": "table containing the column", "column": "column_name or *", "date_bucket": null or "month"/"quarter"/"year", "aggregate": null or "COUNT"/"SUM"/"AVG"/"MIN"/"MAX", "distinct": false, "round_digits": null or 0-6, "alias": "short_snake_case_result_name" }
    { "table": "table containing the column", "column": "column_name or *", "aggregate": "SUM" or "COUNT", "period": { "table": "date column table", "column": "date/datetime/timestamp column", "start": "inclusive YYYY-MM-DD", "end": "exclusive YYYY-MM-DD" }, "alias": "short_snake_case_result_name" }
    { "table": "numeric measure table", "column": "numeric measure column", "aggregate": "SUM", "period_comparison": { "later_period": { "table": "date column table", "column": "date column", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" }, "earlier_period": { "table": "same date table", "column": "same date column", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" } }, "alias": "difference alias" }
    { "metric": "registered metric name", "aggregate": null or "AVG"/"SUM"/"MIN"/"MAX", "alias": "short_snake_case_result_name" }
  ],
  "where_expression": null, a column or registered-metric predicate object, or a boolean group such as {
    "boolean": "AND" or "OR",
    "conditions": [
      { "table": "table containing the column", "column": "column_name", "operator": "one of the allowed operators", "value": "comparison value" },
      { "metric": "registered metric name", "operator": "one of the allowed operators", "value": "finite numeric value" },
      { "boolean": "AND" or "OR", "conditions": ["nested expressions"] }
    ]
  },
  "group_by": [ { "table": "table containing the column", "column": "column_name", "date_bucket": null or "month"/"quarter"/"year" } ],
  "having_expression": null, one aggregate predicate object, or a boolean group such as {
    "boolean": "AND" or "OR",
    "conditions": [
      { "aggregate": "COUNT"/"SUM"/"AVG"/"MIN"/"MAX", "table": "table containing the column", "column": "column_name or *", "operator": "one of the allowed operators", "value": "comparison value" },
      { "aggregate": "SUM"/"AVG"/"MIN"/"MAX", "metric": "registered metric name", "operator": "one of the allowed operators", "value": "finite numeric value" }
    ]
  },
  "search": null or { "term": "literal user phrase, at most 100 characters", "scope": "shops" },
  "count_groups": false,
  "count_entity": null or { "table": "entity table being counted", "column": "its single-column primary key" },
  "order_by": [ { "table": "table containing the column, if ordering by a source column", "column": "column_name or selected alias", "direction": "ASC" or "DESC" } ],
  "limit": null or positive integer requested by the user
}

Answer quality rules:
- Every select_item MUST have a short, human-readable alias in snake_case (e.g. "shop_count", "category_name", "max_price"). Never leave alias null.
- Alias syntax is lowercase snake_case, for example "shop_count" (not "ShopCount").
- When the user asks for an entity name and the name is in a related table, join by the verified relationship and select the name instead of the foreign-key ID.
- Only select an ID column if the question asks for it or it is needed for a correct DISTINCT count.
- For "per X" / "each X" / "by X" questions: use group_by, and order_by the selected count/sum alias DESC. Date-bucket groups must use the same bucket for the selected dimension and grouping key.
- Put row-level filters in where_expression and aggregate thresholds in
  having_expression. Never substitute a row-level rating filter for a threshold
  on AVG(rating).
- Represent a single filter as a predicate object. Use nested boolean groups only
  for two or more predicates; preserve parentheses and the user's AND/OR meaning.
- Set count_groups=true only when the user asks for the number of groups/entities
  that satisfy a HAVING condition. In that case, select exactly one COUNT(*) with
  an alias, include the grouping key(s) and HAVING expression, and count the
  qualifying groups—not matching source rows. The application compiles this to
  a validated grouped subquery.
- For questions asking how many entities are in each dimension (for example,
  shops in each category or active shops across categories), use the dimension
  table as the base (e.g. categories) and LEFT JOIN the counted entity (e.g. shop_details)
  through a verified relationship.
  Filters on the joined child entity (such as shop_details.status = 1) MUST be placed
  inside that join's "conditions" array (e.g. [{"table": "shop_details", "column": "status", "operator": "=", "value": 1}]),
  so they compile into the ON clause.
  NEVER place a filter on the LEFT JOIN child table in where_expression (unless checking its primary key IS NULL),
  because filtering a LEFT JOIN child in WHERE eliminates dimensions with zero entities.
  Count the child's primary key (e.g. COUNT(shop_details.id)) so dimensions with zero matching entities appear as 0.
  For "categories with no active shops", group by category and filter using having_expression:
  {"aggregate": "COUNT", "table": "shop_details", "column": "id", "operator": "=", "value": 0}.
  Show the dimension's name rather than its foreign-key ID when exactly one
  name column is available on the verified related dimension table.
- For lists of records: order_by the name column ASC unless the question implies another order.
- Preserve an explicit requested row limit in "limit". For numeric aggregate
  results, set round_digits only when the question or requested output specifies
  decimal rounding; do not round counts.
- Preserve the original intent and literal values. Do not replace a requested
  schema field (such as city) with a synonym, infer a status field from wording
  like "currently active", or invent a column. If the business meaning needs an
  unsupported date or status definition, mark the query unanswerable.
- If a view (table name ending in "_view") matches the question, prefer it over the raw base table ONLY when the view alone can answer it. A view that has no verified relationship in verified_relationships cannot be joined, so if the question also needs data from another table (for example city, address, or reviews), use the raw base table and its verified relationships instead.
- Every column reference must identify its owning table. COUNT(*) may use "*" without a table; COUNT(DISTINCT ...) must name both table and column.
- For joined COUNT results, count_entity must match the exact table and primary-key column inside every COUNT aggregate. Use null if the query does not contain a joined COUNT.
- For every query without a joined COUNT, set count_entity to null, including single-table grouped counts.
- The schema metadata key="PRI" identifies a primary-key column.
- Every join's relationship object must exactly match a provided relationship,
  including its constraint_name and cardinality. Use an empty joins array when
  no join is needed.

CRITICAL: "reasoning" is REQUIRED in every single response, with no exceptions — including simple questions where the answer feels obvious. A response missing "reasoning" will be rejected by validation and treated as a system failure.

If there is no row filter or aggregate filter, set where_expression and
having_expression to null. If there is no text search, set search to null.
If there is no grouping or ordering, use empty arrays.
Always set count_groups to false unless counting qualifying groups."""

SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "{RATING_RANKING_POLICY}", RATING_RANKING_POLICY
)


def build_generate_query_messages(
    rewritten_question: str,
    schema_context: dict,
    matched_business_rules: list,
    relationships: list | None = None,
    original_question: str | None = None,
    catalog_filters: list[dict] | None = None,
    catalog_filter_combiner: str | None = None,
) -> list[dict]:
    """
    Builds the message list for the query generation LLM call.

    schema_context: table names mapped to their exact column metadata.

    matched_business_rules: list of matched rule dicts from Node 5, with
    date tokens already resolved to real ISO timestamps.
    relationships: database-declared or manually reviewed logical edges.
    """
    if not rewritten_question or not rewritten_question.strip():
        raise ValueError("rewritten_question must not be empty")
    if not schema_context:
        raise ValueError("schema_context must not be empty — schema_grounding.py must run before Node 6")

    context = {
        "original_question": (original_question or rewritten_question).strip(),
        "clarified_question": rewritten_question.strip(),
        "schema": schema_context,
        "verified_relationships": relationships or [],
        "business_rules": matched_business_rules,
        "verified_business_type_filters": catalog_filters or [],
        "verified_business_type_filter_combiner": (
            catalog_filter_combiner
            or ("OR" if len([
                item for item in catalog_filters or []
                if not item.get("exclude")
            ]) > 1 else "AND")
        ),
    }

    user_content = (
        "Here is the context for this question:\n\n"
        f"{json.dumps(context, indent=2, default=str)}"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]