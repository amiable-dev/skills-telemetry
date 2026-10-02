-- Q5 — how much of a skill's tail is cache CREATION, not reuse.   version: 2
--
-- ANSWERS: per skill, the share of its tokens that were spent writing new cache
-- entries rather than reading existing ones. Since #117 the tokens are the
-- harness's own requests named for the skill (`skill_request_cost`), not the
-- tail-rule estimate. A skill whose
-- body changes the prefix of every subsequent request pays cache-creation on
-- each of them; a high share is the signature of a skill that invalidates the
-- cache, which is a refine signal no token total shows on its own.
--
-- DOES NOT PROVE:
--  * causation. A request is the skill's because the harness named it so while
--    the skill was active; a large file read inside the skill is the skill's.
--  * that a high share is bad. A skill loaded once per session legitimately
--    creates cache once; the share is high because the denominator is small.
--    Read it beside n_prompts and skill_tokens, never alone.
--  * comparability with a sub-agent. Sub-agent cache_read is large by
--    construction (ADR-009) and this ratio would be meaningless there, which is
--    why this query is restricted to kind = 'skill'.
--  * anything for a prompt with no native record. n_unmeasured counts the prompts
--    where the skill ran but the harness's own telemetry was off; they are not
--    zero-cost prompts and do not enter the ratio (ADR-005).
--
-- Parameters: :since, :until
WITH c AS (
  SELECT * FROM skill_request_cost
  WHERE first_request_at >= :since AND first_request_at < :until
),
spend AS (
  SELECT skill_name, harness,
         count(*)                         AS n_prompts,
         count(DISTINCT session_id)       AS n_sessions,
         sum(requests)                    AS n_requests,
         sum(requests_derived)            AS n_requests_derived,
         sum(cache_creation_tokens)       AS cache_creation_tokens,
         sum(cache_read_tokens)           AS cache_read_tokens,
         sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)
             + coalesce(cache_read_tokens, 0) + coalesce(cache_creation_tokens, 0)) AS skill_tokens
  FROM c GROUP BY skill_name, harness
),
unmeasured AS (
  SELECT skill_name, harness, count(DISTINCT (session_id, prompt_id)) AS n_unmeasured
  FROM skill_activation_cost
  WHERE abandoned IS NULL AND started_at >= :since AND started_at < :until
  GROUP BY skill_name, harness
),
k AS (SELECT skill_name, harness FROM spend UNION SELECT skill_name, harness FROM unmeasured)
SELECT
  k.skill_name,
  k.harness,
  coalesce(s.n_prompts, 0)                       AS n_prompts,
  s.n_sessions,
  s.n_requests,
  s.n_requests_derived,
  coalesce(u.n_unmeasured, 0)                    AS n_unmeasured,
  s.cache_creation_tokens,
  s.cache_read_tokens,
  s.skill_tokens,
  round(s.cache_creation_tokens::numeric / nullif(s.skill_tokens, 0), 4) AS cache_creation_share
FROM k
LEFT JOIN spend s USING (skill_name, harness)
LEFT JOIN unmeasured u USING (skill_name, harness)
ORDER BY cache_creation_share DESC NULLS LAST, skill_tokens DESC NULLS LAST;
