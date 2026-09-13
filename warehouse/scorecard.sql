-- Weekly skill scorecard (design §4.5). Parameters: :week_start, :week_end
WITH inv AS (
  SELECT * FROM skill_invocation WHERE started_at >= :week_start AND started_at < :week_end
),
-- first-time policy pass for each PR/policy the skill is coupled with
first_run AS (
  SELECT pr_id, policy_id, passed FROM policy_result WHERE run_seq = 1
),
pr_skill AS (   -- PRs whose ticket had ≥1 invocation of the skill
  SELECT DISTINCT p.pr_id, i.skill_name, i.skill_version, i.harness
  FROM pull_request p JOIN inv i ON i.ticket_id = p.ticket_id
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
    ON fr.pr_id = ps.pr_id AND fr.policy_id = sp.policy_id
  GROUP BY 1,2,3,4
),
pass_with AS (
  SELECT skill_name, skill_version, harness,
         AVG(pr_passed::numeric) AS first_pass_rate_with,
         COUNT(*) AS n_with                      -- PRs, never invocations
  FROM pr_pass_with GROUP BY 1,2,3
),
-- the same measure over PRs in the window that did NOT use the skill
pr_pass_without AS (
  SELECT sp.skill_name, p.pr_id,
         MIN(CASE WHEN fr.passed THEN 1 ELSE 0 END) AS pr_passed
  FROM skill_policy sp
  JOIN first_run fr ON fr.policy_id = sp.policy_id
  JOIN pull_request p ON p.pr_id = fr.pr_id
  WHERE p.opened_at >= :week_start AND p.opened_at < :week_end
    AND NOT EXISTS (SELECT 1 FROM pr_skill ps WHERE ps.pr_id = p.pr_id AND ps.skill_name = sp.skill_name)
  GROUP BY 1,2
),
pass_without AS (
  SELECT skill_name, AVG(pr_passed::numeric) AS first_pass_rate_without,
         COUNT(*) AS n_without                   -- PRs, never invocations
  FROM pr_pass_without GROUP BY 1
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
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY i.load_tokens) AS p50_load_tokens,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY i.tail_tokens) AS p50_tail_tokens,
  SUM(i.load_tokens + i.tail_tokens)                  AS total_tokens,
  AVG(CASE WHEN i.is_error THEN 1 ELSE 0 END)         AS error_rate,
  AVG(CASE WHEN i.llm_requests = 0 THEN 1 ELSE 0 END) AS abandonment_rate,
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
GROUP BY 1,2,3, pw.first_pass_rate_with, pw.n_with, pwo.first_pass_rate_without, pwo.n_without, o.co_invoked_skills
ORDER BY total_tokens DESC;
