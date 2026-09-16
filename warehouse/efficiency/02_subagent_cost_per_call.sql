-- Q2 — what one call to each sub-agent type costs.        version: 1
--
-- ANSWERS: per sub-agent type, how many calls were recorded in the window, over
-- how many sessions, and the distribution of tokens per call — cache reads kept
-- in their own columns, because they are not the same measurement.
--
-- DOES NOT PROVE:
--  * dollars. "Cost" here is tokens. Money exists only per session, and prices
--    differ per model; a per-call price would have to be invented.
--  * that one type is worth more than another. This is spend, not outcome. The
--    outcome question is the primary metric, at PR grain, with its own floor.
--  * that a type's cache_read is waste. A sub-agent re-reads the parent context
--    on every request by construction, so cache_read scales with the call count
--    and the parent's size, not with what the sub-agent did.
--  * completeness. A sub-agent still running at the parent's Stop has no span
--    until the next Stop, so the newest window under-counts (ADR-009).
--
-- Parameters: :since, :until
SELECT
  name                                                        AS subagent_type,
  count(*)                                                    AS n_calls,
  count(*) OVER ()                                            AS n_rows,
  count(DISTINCT session_id)                                  AS n_sessions,
  count(DISTINCT parent_prompt_id)
    FILTER (WHERE parent_prompt_id IS NOT NULL)               AS n_parent_turns,
  count(*) FILTER (WHERE output_tokens IS NULL)               AS n_calls_without_usage,
  count(*) FILTER (WHERE source = 'transcript')               AS n_inferred_from_transcript,
  -- percentile_cont ignores NULLs, so these are quantiles over the calls whose
  -- usage was actually observed — n_calls_without_usage says how many were not
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY input_tokens + output_tokens + cache_creation_tokens)   AS p50_tokens_excl_cache_read,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY input_tokens + output_tokens + cache_creation_tokens)   AS p95_tokens_excl_cache_read,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY cache_read_tokens)     AS p50_cache_read_tokens,
  sum(input_tokens)                                           AS input_tokens,
  sum(output_tokens)                                          AS output_tokens,
  sum(cache_creation_tokens)                                  AS cache_creation_tokens,
  sum(cache_read_tokens)                                      AS cache_read_tokens,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY llm_requests)   AS p50_llm_requests,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY tool_calls)     AS p50_tool_calls,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)    AS p50_duration_ms,
  max(ended_at)                                               AS last_seen
FROM artefact_activation
WHERE kind = 'subagent'
  AND started_at >= :since AND started_at < :until
GROUP BY name
ORDER BY coalesce(sum(output_tokens), 0) DESC, n_calls DESC;
