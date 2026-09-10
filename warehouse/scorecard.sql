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
pass_with AS (
  SELECT ps.skill_name, ps.skill_version, ps.harness,
         AVG(CASE WHEN fr.passed THEN 1 ELSE 0 END) AS first_pass_rate_with, COUNT(*) AS n_with
  FROM pr_skill ps
  JOIN inv i ON i.skill_name = ps.skill_name AND i.skill_version = ps.skill_version
  JOIN first_run fr ON fr.pr_id = ps.pr_id AND fr.policy_id = ANY(i.policy_ids)
  GROUP BY 1,2,3
),
pass_without AS (   -- same policies, PRs in the window with no invocation of that skill
  SELECT i.skill_name, AVG(CASE WHEN fr.passed THEN 1 ELSE 0 END) AS first_pass_rate_without, COUNT(*) AS n_without
  FROM (SELECT DISTINCT skill_name, unnest(policy_ids) AS policy_id FROM inv) i
  JOIN first_run fr ON fr.policy_id = i.policy_id
  JOIN pull_request p ON p.pr_id = fr.pr_id
  WHERE p.opened_at >= :week_start AND p.opened_at < :week_end
    AND NOT EXISTS (SELECT 1 FROM pr_skill ps WHERE ps.pr_id = p.pr_id AND ps.skill_name = i.skill_name)
  GROUP BY 1
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
    WHEN COUNT(*) = 0 THEN 'deprecate'
    WHEN COUNT(DISTINCT i.user_hash) = 1 THEN 'review-single-user'
    WHEN pw.first_pass_rate_with IS NOT NULL AND pw.first_pass_rate_with < COALESCE(pwo.first_pass_rate_without, 0) THEN 'refine'
    WHEN COALESCE(o.co_invoked_skills, 0) >= 3 THEN 'review-merge'
    ELSE 'keep'
  END AS recommended_action
FROM inv i
LEFT JOIN pass_with pw ON pw.skill_name = i.skill_name AND pw.skill_version = i.skill_version AND pw.harness = i.harness
LEFT JOIN pass_without pwo ON pwo.skill_name = i.skill_name
LEFT JOIN overlap o ON o.skill_name = i.skill_name
GROUP BY 1,2,3, pw.first_pass_rate_with, pw.n_with, pwo.first_pass_rate_without, pwo.n_without, o.co_invoked_skills
ORDER BY total_tokens DESC;
