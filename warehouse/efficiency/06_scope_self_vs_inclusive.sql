-- Q6 — what a scoped run cost, self against inclusive.     version: 2
--
-- ANSWERS: for one scoping artefact, what each iteration and the whole run
-- carried, split into the artefact's own activations (self) and everything that
-- ran inside its container (inclusive). This is the question ADR-010 exists for:
-- a loop skill's own activation is seconds of tool call while the work it drives
-- is the largest line item, and only the second column shows that.
--
-- The split is derived here, never stored. No row holds a total that includes
-- its children (ADR-010 decision 6): storing both as peers is how double
-- counting becomes structural, and this project already has one documented pair
-- that must never be summed.
--
-- DOES NOT PROVE:
--  * causation. This is what was *incurred under* the artefact, not what the
--    artefact *caused*. Everything in the window inherits the scope, including
--    an unrelated question typed mid-loop, because there is no observable causal
--    link from a skill activation to a later tool call. Keep / refine / merge /
--    deprecate stays on the with-and-without arm at PR grain.
--  * anything for a prompt with no native record. Since #117 every figure is the
--    harness's own requests (input + output tokens), each counted once however
--    many spans share its prompt; `self` is those the harness named for the
--    scoping skill. A prompt in scope with no native record is counted in
--    prompts_without_usage, never as zero.
--  * that a declared scope is what ran. `scope_source = 'overlay'` means an
--    operator asserted this on a third party's behalf and it can be stale.
--  * that iterations are comparable. One ticket may be a typo fix and the next a
--    migration; n_turns is here so that is visible rather than assumed.
--
-- Parameters: :scope_name, :since
WITH scoped AS (
  SELECT * FROM artefact_activation
  WHERE scope_name = :scope_name AND started_at >= :since
),
prompts AS (
  SELECT DISTINCT scope_key, session_id, prompt_id FROM scoped WHERE prompt_id IS NOT NULL
),
req AS (           -- each request once, however many activations share its prompt
  SELECT p.scope_key, r.skill_name,
         coalesce(r.input_tokens, 0) + coalesce(r.output_tokens, 0) AS tokens
  FROM prompts p
  JOIN llm_request_attributed r ON r.session_id = p.session_id AND r.prompt_id = p.prompt_id
),
spend AS (
  SELECT scope_key,
         -- a native name may carry a plugin prefix (`acme:loop`)
         sum(tokens) FILTER (WHERE skill_name = :scope_name
                               OR split_part(skill_name, ':', 2) = :scope_name) AS self_tokens,
         sum(tokens)                                                     AS request_tokens
  FROM req GROUP BY scope_key
),
-- spend the harness cannot see (ADR-010 decision 7) is not in llm_request
external AS (
  SELECT scope_key, sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens
  FROM scoped WHERE kind = 'external' GROUP BY scope_key
),
coverage AS (
  SELECT p.scope_key,
         count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM llm_request r WHERE r.session_id = p.session_id
                                                                           AND r.prompt_id = p.prompt_id))
                    AS prompts_without_usage,
         count(*)  AS prompts_in_scope
  FROM prompts p GROUP BY p.scope_key
),
shape AS (
  SELECT scope_key,
         min(scope_source)                                      AS scope_source,
         count(DISTINCT scope_id)                               AS n_runs,
         count(DISTINCT prompt_id) FILTER (WHERE kind = 'turn') AS n_turns
  FROM scoped GROUP BY scope_key
)
SELECT
  sh.scope_key, sh.scope_source, sh.n_runs, sh.n_turns,
  coalesce(sp.self_tokens, 0)                                     AS self_tokens,
  -- inclusive: every request in a scoped prompt, plus external spend. A turn's
  -- own token columns are not added: they are the same requests again.
  coalesce(sp.request_tokens, 0) + coalesce(e.tokens, 0)          AS inclusive_tokens,
  coalesce(cv.prompts_without_usage, 0)                           AS prompts_without_usage,
  coalesce(cv.prompts_in_scope, 0)                                AS prompts_in_scope
FROM shape sh
LEFT JOIN spend sp    USING (scope_key)
LEFT JOIN external e  USING (scope_key)
LEFT JOIN coverage cv USING (scope_key)
ORDER BY inclusive_tokens DESC;
