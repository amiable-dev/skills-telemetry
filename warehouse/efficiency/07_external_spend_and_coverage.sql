-- Q7 — what spend happened outside the harness, and how much of it we know.  version: 1
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
SELECT
  external_system,
  external_operation,
  count(*)                                                   AS runs,
  count(external_cost_usd)                                   AS runs_with_cost,
  -- count(col) skips NULL, so this is coverage rather than a guess
  round(100.0 * count(external_cost_usd) / nullif(count(*), 0), 1) AS coverage_pct,
  round(sum(external_cost_usd), 4)                           AS cost_usd_known,
  round(avg(external_cost_usd), 4)                           AS mean_cost_of_known,
  sum(external_requests)                                     AS model_requests,
  sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens,
  count(*) FILTER (WHERE scope_name IS NOT NULL)             AS runs_inside_a_scope
FROM artefact_activation
WHERE kind = 'external' AND started_at >= :since
GROUP BY external_system, external_operation
ORDER BY cost_usd_known DESC NULLS LAST;
