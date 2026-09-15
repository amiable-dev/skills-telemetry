-- Q3 — how often context compacts, and what preceded each one.   version: 1
--
-- ANSWERS: one row per compaction in the window, with the harness's own
-- before/after estimates, the gap since the previous compaction in the same
-- session, and what happened in that gap — turns, sub-agent calls, skill loads
-- and the tokens those turns reported.
--
-- DOES NOT PROVE:
--  * that what preceded a compaction caused it. This is what was in the window,
--    in time order. A sub-agent that ran just before a compaction is a lead, not
--    a cause; the same session would compact eventually regardless.
--  * exact token figures. tokens_before / tokens_after are the harness's own
--    ESTIMATES and are recorded as received, never adjusted. Where the harness
--    did not supply them they are NULL, and tokens_dropped is NULL too — a
--    compaction that dropped nothing is not the same as one we could not measure.
--  * the first compaction's gap. minutes_since_previous is NULL for the first
--    compaction seen in the window, which may not be the session's first.
--
-- Parameters: :since, :until
WITH c AS (
  SELECT span_id, session_id, started_at, ended_at, ticket_id, harness,
         compaction_reason, compaction_tokens_before, compaction_tokens_after,
         compaction_turns_since_previous, source,
         lag(started_at) OVER (PARTITION BY session_id ORDER BY started_at) AS previous_at
  FROM artefact_activation
  WHERE kind = 'compaction'
    AND started_at >= :since AND started_at < :until
)
SELECT
  count(*) OVER ()                                      AS n_rows,
  count(*) OVER (PARTITION BY c.session_id)             AS n_compactions_in_session,
  c.session_id,
  c.started_at,
  c.harness,
  c.ticket_id,
  c.compaction_reason,
  c.source,
  c.compaction_tokens_before,
  c.compaction_tokens_after,
  c.compaction_tokens_before - c.compaction_tokens_after AS tokens_dropped,
  c.compaction_turns_since_previous                      AS turns_since_previous_reported,
  round(extract(epoch FROM (c.started_at - c.previous_at)) / 60.0, 1)
                                                         AS minutes_since_previous,
  p.n_turns_before,
  p.n_subagent_calls_before,
  p.n_skill_loads_before,
  p.turn_tokens_before
FROM c
LEFT JOIN LATERAL (
  SELECT
    -- one row per Stop, so the turn count is over distinct prompt_ids
    count(DISTINCT a.prompt_id) FILTER (WHERE a.kind = 'turn')  AS n_turns_before,
    count(*) FILTER (WHERE a.kind = 'subagent')                 AS n_subagent_calls_before,
    count(*) FILTER (WHERE a.kind = 'skill')                    AS n_skill_loads_before,
    sum(a.input_tokens + a.output_tokens) FILTER (WHERE a.kind = 'turn')
                                                                AS turn_tokens_before
  FROM artefact_activation a
  WHERE a.session_id = c.session_id
    AND a.started_at < c.started_at
    AND (c.previous_at IS NULL OR a.started_at >= c.previous_at)
) p ON TRUE
ORDER BY c.started_at;
