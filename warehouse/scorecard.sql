-- Weekly skill scorecard (design §4.5). Parameters: :week_start, :week_end
WITH inv AS (
  SELECT * FROM skill_invocation WHERE started_at >= :week_start AND started_at < :week_end
),
-- first-time policy pass for each change request/policy the skill is coupled with
first_run AS (
  SELECT cr_id, policy_id, passed FROM policy_result WHERE run_seq = 1
),
-- ADR-013: the change request each skill activation's work became. The with-arm
-- needs commit evidence (`branch+commit`, or `commit` alone); a skill linked to a
-- change request by branch alone is in NEITHER arm (skill_uncertain), never
-- recruited into the without-arm — ADR-002's "unknown, never none", applied to
-- the weaker join.
attributed AS (
  SELECT v.cr_id, v.method, i.skill_name, i.skill_version, i.harness
  FROM inv i
  JOIN activation_change_request v ON v.span_id = i.span_id
  JOIN change_request c ON c.cr_id = v.cr_id AND c.state = 'merged'
),
pr_skill AS (   -- merged change requests the skill demonstrably contributed to
  SELECT DISTINCT cr_id AS pr_id, skill_name, skill_version, harness
  FROM attributed WHERE method IN ('branch+commit', 'commit')
),
skill_uncertain AS (   -- linked by branch only: excluded from both arms
  SELECT DISTINCT a.cr_id AS pr_id, a.skill_name
  FROM attributed a
  WHERE a.method = 'branch'
    AND NOT EXISTS (SELECT 1 FROM pr_skill p WHERE p.pr_id = a.cr_id AND p.skill_name = a.skill_name)
),
-- the policies each skill is accountable for, one row per (skill, version, policy)
skill_policy AS (
  SELECT DISTINCT skill_name, skill_version, unnest(policy_ids) AS policy_id FROM inv
),
-- A PR passes first time when EVERY policy the skill is accountable for passed on
-- run_seq = 1. Aggregating to one row per PR first is the whole point: the metric
-- is "first-time pass rate on the PR", so the PR is the unit. Averaging over the
-- join directly counted each PR once per invocation and reported n_with = 3192
-- against 120 merged PRs (#23).
pr_pass_with AS (
  SELECT ps.skill_name, ps.skill_version, ps.harness, ps.pr_id,
         MIN(CASE WHEN fr.passed THEN 1 ELSE 0 END) AS pr_passed
  FROM pr_skill ps
  JOIN skill_policy sp
    ON sp.skill_name = ps.skill_name AND sp.skill_version = ps.skill_version
  JOIN first_run fr
    ON fr.cr_id = ps.pr_id AND fr.policy_id = sp.policy_id
  GROUP BY 1,2,3,4
),
pass_with AS (
  SELECT skill_name, skill_version, harness,
         AVG(pr_passed::numeric) AS first_pass_rate_with,
         COUNT(*) AS n_with                      -- PRs, never invocations
  FROM pr_pass_with GROUP BY 1,2,3
),
-- the same measure over merged change requests in the window that did NOT use
-- the skill — excluding those linked to it by branch alone, which are unknown
pr_pass_without AS (
  SELECT sp.skill_name, c.cr_id AS pr_id,
         MIN(CASE WHEN fr.passed THEN 1 ELSE 0 END) AS pr_passed
  FROM skill_policy sp
  JOIN first_run fr ON fr.policy_id = sp.policy_id
  JOIN change_request c ON c.cr_id = fr.cr_id AND c.state = 'merged'
  WHERE c.opened_at >= :week_start AND c.opened_at < :week_end
    AND NOT EXISTS (SELECT 1 FROM pr_skill ps WHERE ps.pr_id = c.cr_id AND ps.skill_name = sp.skill_name)
    AND NOT EXISTS (SELECT 1 FROM skill_uncertain su WHERE su.pr_id = c.cr_id AND su.skill_name = sp.skill_name)
  GROUP BY 1,2
),
pass_without AS (
  SELECT skill_name, AVG(pr_passed::numeric) AS first_pass_rate_without,
         COUNT(*) AS n_without                   -- PRs, never invocations
  FROM pr_pass_without GROUP BY 1
),
-- #117: what the skill cost, from the harness's own requests named for it. One
-- row per (session, prompt, skill, version) in skill_activation_cost, so a skill
-- run twice in a prompt is not charged twice. `abandoned` is NULL where the
-- prompt has no native record, and those prompts are counted, never averaged in.
cost AS (
  SELECT skill_name, skill_version, harness,
         PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tokens)  AS p50_tokens,
         SUM(tokens)                                          AS total_tokens,
         SUM(cost_usd)                                        AS total_cost_usd,
         SUM(requests_derived)                                AS requests_derived,
         AVG(CASE WHEN abandoned THEN 1 WHEN NOT abandoned THEN 0 END) AS abandonment_rate,
         COUNT(*) FILTER (WHERE abandoned IS NULL)            AS unmeasured_activations
  FROM (SELECT *, input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens AS tokens
        FROM skill_activation_cost
        WHERE started_at >= :week_start AND started_at < :week_end) a
  GROUP BY 1,2,3
),
overlap AS (   -- redundancy: co-invocation with other skills in the same session
  SELECT a.skill_name, COUNT(DISTINCT b.skill_name) AS co_invoked_skills
  FROM inv a JOIN inv b ON a.session_id = b.session_id AND a.skill_name <> b.skill_name
  GROUP BY 1
)
SELECT
  i.skill_name, i.skill_version, i.harness,
  COUNT(*)                                            AS invocations,
  COUNT(DISTINCT i.user_hash)                         AS distinct_users,
  COUNT(DISTINCT i.session_id)                        AS sessions,
  c.p50_tokens, c.total_tokens, c.total_cost_usd, c.requests_derived,
  AVG(CASE WHEN i.is_error THEN 1 ELSE 0 END)         AS error_rate,
  c.abandonment_rate, c.unmeasured_activations,
  pw.first_pass_rate_with, pw.n_with,
  pwo.first_pass_rate_without, pwo.n_without,
  COALESCE(o.co_invoked_skills, 0)                    AS co_invoked_skills,
  CASE
    -- Insufficient data is checked FIRST and is not a verdict about the skill.
    -- 30 merged PRs per arm is the floor from docs/evaluation-power.md; below
    -- it no comparative claim is supportable, and 'keep' used to be returned
    -- for a skill with no outcome data at all (#23).
    WHEN pw.first_pass_rate_with IS NULL OR pwo.first_pass_rate_without IS NULL
      THEN 'insufficient-data'
    WHEN COALESCE(pw.n_with, 0) < 30 OR COALESCE(pwo.n_without, 0) < 30
      THEN 'insufficient-data'
    WHEN COUNT(*) = 0 THEN 'deprecate'
    WHEN COUNT(DISTINCT i.user_hash) = 1 THEN 'review-single-user'
    WHEN pw.first_pass_rate_with < pwo.first_pass_rate_without THEN 'refine'
    WHEN COALESCE(o.co_invoked_skills, 0) >= 3 THEN 'review-merge'
    ELSE 'keep'
  END AS recommended_action
FROM inv i
LEFT JOIN pass_with pw ON pw.skill_name = i.skill_name AND pw.skill_version = i.skill_version AND pw.harness = i.harness
LEFT JOIN pass_without pwo ON pwo.skill_name = i.skill_name
LEFT JOIN overlap o ON o.skill_name = i.skill_name
LEFT JOIN cost c ON c.skill_name = i.skill_name AND c.skill_version = i.skill_version AND c.harness = i.harness
GROUP BY 1,2,3, pw.first_pass_rate_with, pw.n_with, pwo.first_pass_rate_without, pwo.n_without, o.co_invoked_skills,
         c.p50_tokens, c.total_tokens, c.total_cost_usd, c.requests_derived, c.abandonment_rate,
         c.unmeasured_activations
ORDER BY c.total_cost_usd DESC NULLS LAST, invocations DESC;
