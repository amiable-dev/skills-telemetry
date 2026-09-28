-- Q7 — what spend happened outside the harness, and how much of it we know.  version: 3
--
-- ANSWERS: for each external emitter, how many runs it made, what they cost,
-- and — the part that matters — on how many of those runs a cost was reported
-- at all. This is the only place in the schema carrying a currency amount,
-- because it is the only place where the emitter knows something no hook can
-- observe (ADR-010 decision 7).
--
-- `coverage` is not decoration. A total computed over a file where most runs
-- report nothing is an average of the part that was measured, not of the work
-- that was done, and it will not reconcile against a provider's invoice. Read
-- the coverage before reading the total; if it is not complete, the total is a lower
-- bound and should be described as one.
--
-- ESTIMATES (version 2, issue #88) are a separate column and never enter
-- `coverage_pct` or `cost_usd_known`. Coverage exists to reconcile against an
-- invoice, and a figure priced from a list is not on one. `runs_estimated_only`
-- is spend we now see instead of losing. `cost_usd_estimated` is every estimated
-- amount, including the estimated part of a mixed run whose billed part is in
-- `cost_usd_known` — so the two columns do not overlap and may be added, but
-- only if you say you did, and never call the sum a bill.
-- `runs_unlabelled` carries an observed cost with no `cost_source` — emitters
-- from before the label existed. They are not assumed to be `provider`.
--
-- NO SESSION: an emitter outside Claude Code (CLI, HTTP, CI) sends no
-- `session.id`, and the row loads with session_id = ''. That is a category, not a
-- key: '' joins to '', so any query that groups or joins by session would merge
-- every such run into one fictitious session. Never join these rows by session,
-- turn or scope. They are attributable to system and operation only, and
-- `runs_outside_a_session` counts them so they are seen rather than hidden.
--
-- PARTIAL RUNS (version 3, ADR-012). A run whose `requests_unpriced` is above
-- zero had requests with neither an observed nor an estimated cost, so its
-- observed figure is a lower bound. It is kept rather than discarded, which is
-- what version 2 forced, and it is kept apart: `runs_with_cost`, `coverage_pct`,
-- `cost_usd_known` and its mean are complete runs only, and `cost_usd_partial`
-- is the lower bounds. A row with no count predates version 3 and keeps its
-- version 2 reading.
--
-- THE JOIN (version 3, ADR-012). An MCP emitter's `session.id` is the session
-- that started its server, and goes stale after `/clear`. A run carrying
-- `tool_use_id` is joined through `mcp_tool_call` to the turn or sub-agent that
-- made the call, so its scope is the scope of the turn that caused it.
-- `runs_joined_to_a_call` against `runs_with_a_tool_use_id` is the join rate: a
-- falling rate means the undocumented `_meta` key the join relies on has
-- changed, and every query attributing external spend is falling back to the
-- stale id. The emitter's own `session_id` is never rewritten.
--
-- DOES NOT PROVE:
--  * that the total is the bill. Runs the emitter never reported are absent
--    entirely, and absent rows cannot be counted — so this understates by an
--    unknown amount whenever coverage is incomplete.
--  * that a zero cost is an error. Free tiers and cached responses really do
--    cost nothing. Zero observed and nothing observed are different, which is
--    why an unreported cost is NULL here and NULL is excluded from avg().
--  * that this spend was caused by the scope it sits in. Rolling up by
--    `scope_name` says what was incurred under a loop, never what the loop
--    caused (ADR-010 decision 9).
--  * that two emitters are comparable. One `consult` may fan out to nine models
--    and the next to two; `requests` is here so that is visible.
--
-- Parameters: :since
WITH e AS (
  SELECT a.*,
         a.external_cost_usd IS NOT NULL
           AND coalesce(a.external_requests_unpriced, 0) = 0     AS complete,
         a.external_cost_usd IS NOT NULL
           AND a.external_requests_unpriced > 0                 AS partial,
         m.tool_use_id IS NOT NULL                              AS joined,
         coalesce(a.scope_name, t.scope_name)                   AS resolved_scope
  FROM artefact_activation a
  LEFT JOIN mcp_tool_call m      ON m.tool_use_id = a.external_tool_use_id
  LEFT JOIN artefact_activation t ON t.span_id = m.activation_span_id
  WHERE a.kind = 'external' AND a.started_at >= :since
)
SELECT
  external_system,
  external_operation,
  count(*)                                                   AS runs,
  count(*) FILTER (WHERE complete)                           AS runs_with_cost,
  round(100.0 * count(*) FILTER (WHERE complete) / nullif(count(*), 0), 1) AS coverage_pct,
  round(sum(external_cost_usd) FILTER (WHERE complete), 4)   AS cost_usd_known,
  round(avg(external_cost_usd) FILTER (WHERE complete), 4)   AS mean_cost_of_known,
  count(*) FILTER (WHERE partial)                            AS runs_partial,
  round(sum(external_cost_usd) FILTER (WHERE partial), 4)    AS cost_usd_partial,
  count(*) FILTER (WHERE external_cost_usd IS NOT NULL
                     AND external_cost_source IS NULL)       AS runs_unlabelled,
  count(*) FILTER (WHERE external_cost_usd IS NULL
                     AND external_cost_estimated_usd IS NOT NULL) AS runs_estimated_only,
  round(sum(external_cost_estimated_usd), 4)                 AS cost_usd_estimated,
  count(*) FILTER (WHERE session_id = '')                    AS runs_outside_a_session,
  count(*) FILTER (WHERE external_tool_use_id IS NOT NULL)   AS runs_with_a_tool_use_id,
  count(*) FILTER (WHERE joined)                             AS runs_joined_to_a_call,
  sum(external_requests)                                     AS model_requests,
  sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens,
  count(*) FILTER (WHERE resolved_scope IS NOT NULL)         AS runs_inside_a_scope
FROM e
GROUP BY external_system, external_operation
ORDER BY cost_usd_known DESC NULLS LAST;
