-- Q4 — which hook is spending the wall time.               version: 1
--
-- ANSWERS: per hook, how many firings were observed in the window, across how
-- many turns and sessions, and the distribution of its self-reported duration.
-- The hook is identified by the basename of its command; the payload carries an
-- absolute path, which is a developer's filesystem layout and is never recorded.
--
-- DOES NOT PROVE:
--  * that the total is the developer's wait. Hooks for one event run in
--    parallel, and the harness's own overhead is not in this number. Read it as
--    "which hook is expensive", not "this is how much slower the session was".
--  * that a hook not listed is fast. A hook that never fired, or a harness that
--    reported no hookInfos, produces no row — absence here is missing data.
--  * attribution to a hook event. The basename says which script ran, not
--    whether it ran at PreToolUse or Stop; a script bound to several events is
--    one series.
--  * anything about a scripted hook's interpreter. `uv run stdtel-hook` is
--    recorded as the script, not as `uv`, or every hook on the machine would
--    share one series.
--
-- Parameters: :since, :until
WITH fired AS (
  SELECT a.session_id, a.prompt_id, a.started_at,
         h.key                AS hook,
         (h.value)::numeric   AS ms
  FROM artefact_activation a
  CROSS JOIN LATERAL jsonb_each_text(a.hook_ms_by_hook) AS h(key, value)
  WHERE a.kind = 'turn'
    AND a.hook_ms_by_hook IS NOT NULL
    AND a.started_at >= :since AND a.started_at < :until
)
SELECT
  hook,
  count(*)                                                AS n_firings,
  count(*) OVER ()                                        AS n_rows,
  -- turns, not rows: one turn can produce two rows when a Stop repeats
  count(DISTINCT prompt_id)                               AS n_turns,
  count(DISTINCT session_id)                              AS n_sessions,
  round(avg(ms), 1)                                       AS mean_ms,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY ms)         AS p50_ms,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY ms)        AS p95_ms,
  max(ms)                                                 AS max_ms,
  sum(ms)                                                 AS total_ms,
  max(started_at)                                         AS last_seen
FROM fired
GROUP BY hook
ORDER BY total_ms DESC;
