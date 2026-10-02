-- Q1 — tokens by artefact kind, for one session.          version: 2
--
-- ANSWERS: for a single session, how many activations of each kind were recorded,
-- how many distinct turns they cover, and the tokens attributed to each kind,
-- with cache reads reported apart from the rest.
--
-- DOES NOT PROVE:
--  * that the kinds partition the session. They overlap by construction — a
--    skill's requests are also inside the turn that ran them. Read down the
--    column, never across the rows; a total over kinds is not a number.
--  * that the skill row is the same kind of figure as the others. Since #117 a
--    skill activation carries no usage: the skill row's tokens and requests are
--    the harness's own requests named for any skill in the session, and its
--    n_without_usage counts skill runs in prompts with no native record at all
--    (telemetry off: unmeasured, not free).
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
WITH kinds AS (
  SELECT
    kind,
    count(*)                                                AS n_activations,
    -- A turn emits one row per Stop carrying that slice's delta, so two Stops in
    -- one turn produce two rows: they sum correctly and must not be counted twice.
    count(DISTINCT prompt_id) FILTER (WHERE kind = 'turn')  AS n_turns,
    count(DISTINCT name) FILTER (WHERE name IS NOT NULL)    AS n_distinct_names,
    -- how much of the group is missing usage: a sum over 2 of 40 rows is a
    -- different claim from a sum over 40, and NULL is how absence is recorded
    count(*) FILTER (WHERE output_tokens IS NULL)           AS n_without_usage,
    count(*) FILTER (WHERE kind = 'skill' AND NOT EXISTS (
      SELECT 1 FROM llm_request r WHERE r.session_id = a.session_id
                                     AND r.prompt_id = a.prompt_id))
                                                            AS n_skill_unmeasured,
    sum(input_tokens)                                       AS input_tokens,
    sum(output_tokens)                                      AS output_tokens,
    sum(cache_creation_tokens)                              AS cache_creation_tokens,
    sum(cache_read_tokens)                                  AS cache_read_tokens,
    sum(llm_requests)                                       AS llm_requests,
    sum(tool_calls)                                         AS tool_calls,
    sum(duration_ms)                                        AS duration_ms,
    min(started_at)                                         AS first_seen,
    max(ended_at)                                           AS last_seen
  FROM artefact_activation a
  WHERE session_id = :session_id
  GROUP BY kind
),
-- the harness's own requests named for a skill, in this session (#117)
skill_native AS (
  SELECT sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
         sum(cache_creation_tokens) AS cache_creation_tokens,
         sum(cache_read_tokens) AS cache_read_tokens, sum(requests) AS llm_requests
  FROM skill_request_cost WHERE session_id = :session_id
)
SELECT
  k.kind,
  k.n_activations,
  count(*) OVER ()                                        AS n_rows,
  k.n_turns,
  k.n_distinct_names,
  CASE WHEN k.kind = 'skill' THEN k.n_skill_unmeasured ELSE k.n_without_usage END AS n_without_usage,
  CASE WHEN k.kind = 'skill' THEN n.input_tokens          ELSE k.input_tokens          END AS input_tokens,
  CASE WHEN k.kind = 'skill' THEN n.output_tokens         ELSE k.output_tokens         END AS output_tokens,
  CASE WHEN k.kind = 'skill' THEN n.cache_creation_tokens ELSE k.cache_creation_tokens END AS cache_creation_tokens,
  CASE WHEN k.kind = 'skill' THEN n.cache_read_tokens     ELSE k.cache_read_tokens     END AS cache_read_tokens,
  CASE WHEN k.kind = 'skill' THEN n.llm_requests          ELSE k.llm_requests          END AS llm_requests,
  k.tool_calls,
  k.duration_ms,
  k.first_seen,
  k.last_seen
FROM kinds k CROSS JOIN skill_native n
ORDER BY coalesce(CASE WHEN k.kind = 'skill' THEN n.input_tokens ELSE k.input_tokens END, 0)
       + coalesce(CASE WHEN k.kind = 'skill' THEN n.output_tokens ELSE k.output_tokens END, 0)
       + coalesce(CASE WHEN k.kind = 'skill' THEN n.cache_creation_tokens ELSE k.cache_creation_tokens END, 0) DESC;
