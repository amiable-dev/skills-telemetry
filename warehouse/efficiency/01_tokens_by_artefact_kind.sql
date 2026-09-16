-- Q1 — tokens by artefact kind, for one session.          version: 1
--
-- ANSWERS: for a single session, how many activations of each kind were recorded,
-- how many distinct turns they cover, and the tokens attributed to each kind,
-- with cache reads reported apart from the rest.
--
-- DOES NOT PROVE:
--  * that the kinds partition the session. They overlap by construction — a
--    skill's tail tokens are also inside the turn that loaded it. Read down the
--    column, never across the rows; a total over kinds is not a number.
--  * that a sub-agent's cache_read is comparable with a skill's. Every
--    sub-agent request re-reads the parent context, so that figure is large by
--    construction and says nothing about the sub-agent's own work (ADR-009).
--  * money. Cost is reported once per session (session_cost.cost_usd); the
--    harness supplies no per-artefact split, and dividing the session total by
--    tokens would invent one.
--  * that a kind with no row did not happen. A sub-agent still running at Stop
--    has no span yet, and a harness without the hook emits none at all.
--
-- Parameters: :session_id
SELECT
  kind,
  count(*)                                                AS n_activations,
  count(*) OVER ()                                        AS n_rows,
  -- A turn emits one row per Stop carrying that slice's delta, so two Stops in
  -- one turn produce two rows: they sum correctly and must not be counted twice.
  count(DISTINCT prompt_id) FILTER (WHERE kind = 'turn')  AS n_turns,
  count(DISTINCT name) FILTER (WHERE name IS NOT NULL)    AS n_distinct_names,
  -- how much of the group is missing usage: a sum over 2 of 40 rows is a
  -- different claim from a sum over 40, and NULL is how absence is recorded
  count(*) FILTER (WHERE output_tokens IS NULL)           AS n_without_usage,
  sum(input_tokens)                                       AS input_tokens,
  sum(output_tokens)                                      AS output_tokens,
  sum(cache_creation_tokens)                              AS cache_creation_tokens,
  sum(cache_read_tokens)                                  AS cache_read_tokens,
  sum(llm_requests)                                       AS llm_requests,
  sum(tool_calls)                                         AS tool_calls,
  sum(duration_ms)                                        AS duration_ms,
  min(started_at)                                         AS first_seen,
  max(ended_at)                                           AS last_seen
FROM artefact_activation
WHERE session_id = :session_id
GROUP BY kind
ORDER BY coalesce(sum(input_tokens), 0) + coalesce(sum(output_tokens), 0)
       + coalesce(sum(cache_creation_tokens), 0) DESC;
