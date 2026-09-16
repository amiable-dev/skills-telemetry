-- Q5 — how much of a skill's tail is cache CREATION, not reuse.   version: 1
--
-- ANSWERS: per skill, the share of its attributed tail tokens that were spent
-- writing new cache entries rather than reading existing ones. A skill whose
-- body changes the prefix of every subsequent request pays cache-creation on
-- each of them; a high share is the signature of a skill that invalidates the
-- cache, which is a refine signal no token total shows on its own.
--
-- DOES NOT PROVE:
--  * causation. The tail rule attributes every request between this skill's load
--    and the next skill's load to this skill. Anything else in that window —
--    a sub-agent, a large file read, a compaction — is inside the same tail.
--  * that a high share is bad. A skill loaded once per session legitimately
--    creates cache once; the share is high because the denominator is small.
--    Read it beside n_invocations and tail_tokens, never alone.
--  * comparability with a sub-agent. Sub-agent cache_read is large by
--    construction (ADR-009) and this ratio would be meaningless there, which is
--    why this query is restricted to kind = 'skill'.
--  * anything for a skill with no observed usage. n_without_usage counts the
--    invocations whose tail carried no usage attributes at all; those rows are
--    NULL, not zero, and do not enter the ratio.
--
-- Parameters: :since, :until
WITH s AS (
  SELECT
    a.name AS skill_name, a.harness, a.span_id, a.session_id,
    a.input_tokens, a.output_tokens, a.cache_read_tokens, a.cache_creation_tokens,
    -- NULL when nothing was observed, so it cannot masquerade as a zero-token tail
    CASE WHEN a.input_tokens IS NULL AND a.output_tokens IS NULL
          AND a.cache_read_tokens IS NULL AND a.cache_creation_tokens IS NULL
         THEN NULL
         ELSE coalesce(a.input_tokens, 0) + coalesce(a.output_tokens, 0)
            + coalesce(a.cache_read_tokens, 0) + coalesce(a.cache_creation_tokens, 0)
    END AS tail_total
  FROM artefact_activation a
  WHERE a.kind = 'skill'
    AND a.started_at >= :since AND a.started_at < :until
)
SELECT
  skill_name,
  harness,
  count(*)                                       AS n_invocations,
  count(*) OVER ()                               AS n_rows,
  count(DISTINCT session_id)                     AS n_sessions,
  count(*) FILTER (WHERE tail_total IS NULL)     AS n_without_usage,
  sum(cache_creation_tokens)                     AS cache_creation_tokens,
  sum(cache_read_tokens)                         AS cache_read_tokens,
  sum(tail_total)                                AS tail_tokens,
  round(sum(cache_creation_tokens)::numeric / nullif(sum(tail_total), 0), 4)
                                                 AS cache_creation_share
FROM s
GROUP BY skill_name, harness
ORDER BY cache_creation_share DESC NULLS LAST, tail_tokens DESC NULLS LAST;
