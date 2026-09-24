-- Q6 — what a scoped run cost, self against inclusive.     version: 1
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
--  * that the leaf figures are measurements. A skill's share of a turn is the
--    tail heuristic, with `tail_tokens_first_only` as its sensitivity check.
--    Containment became readable; the attribution underneath did not get truer.
--  * that a declared scope is what ran. `scope_source = 'overlay'` means an
--    operator asserted this on a third party's behalf and it can be stale.
--  * that iterations are comparable. One ticket may be a typo fix and the next a
--    migration; n_turns is here so that is visible rather than assumed.
--
-- Parameters: :scope_name, :since
WITH scoped AS (
  SELECT * FROM artefact_activation
  WHERE scope_name = :scope_name AND started_at >= :since
)
SELECT
  scope_key,
  min(scope_source)                                         AS scope_source,
  count(DISTINCT scope_id)                                  AS n_runs,
  count(DISTINCT prompt_id) FILTER (WHERE kind = 'turn')    AS n_turns,
  -- self: the scoping artefact's own activations.
  coalesce(sum(input_tokens + output_tokens)
           FILTER (WHERE kind = 'skill' AND name = :scope_name), 0)  AS self_tokens,
  -- inclusive: everything inside the container. Turns are excluded from the sum
  -- rather than added to it — a turn's tokens already contain the skills and
  -- sub-agents inside it, so adding both counts the same tokens twice, which is
  -- exactly the trap this query's shape is designed to avoid.
  coalesce(sum(input_tokens + output_tokens)
           FILTER (WHERE kind <> 'turn'), 0)                         AS inclusive_tokens,
  -- how much of the group has no usage recorded at all: a sum over 2 of 40 rows
  -- is a different claim from a sum over 40 (ADR-005)
  count(*) FILTER (WHERE input_tokens IS NULL AND kind <> 'turn')    AS rows_without_usage,
  count(*) FILTER (WHERE kind <> 'turn')                             AS rows_in_scope
FROM scoped
GROUP BY scope_key
ORDER BY inclusive_tokens DESC;
