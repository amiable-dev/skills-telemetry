-- Warehouse schema: telemetry joined to delivery and quality (design §4.4).
CREATE TABLE IF NOT EXISTS skill_invocation (
  span_id            TEXT PRIMARY KEY,
  trace_id           TEXT NOT NULL,
  session_id         TEXT NOT NULL,
  started_at         TIMESTAMPTZ NOT NULL,
  ended_at           TIMESTAMPTZ NOT NULL,
  harness            TEXT NOT NULL,          -- claude-code | copilot-vscode | copilot-cli
  harness_mode       TEXT,
  skill_name         TEXT NOT NULL,          -- catalogue name (bare, from SKILL.md front-matter)
  invoked_as         TEXT,                   -- raw invocation string; namespaced when plugin-provided
  plugin             TEXT,                   -- namespace of invoked_as, NULL for a bare skill
  skill_version      TEXT NOT NULL,
  content_hash       TEXT,                   -- SHA-256 of the SKILL.md body: asserted version vs observed content
  standard_id        TEXT,
  policy_ids         TEXT[],
  trigger            TEXT,                   -- caller.type from the transcript ("direct"), or unknown
  model              TEXT,
  load_tokens        INT DEFAULT 0,
  tail_tokens        INT DEFAULT 0,
  tail_tokens_first_only INT DEFAULT 0,
  input_tokens       INT DEFAULT 0,
  output_tokens      INT DEFAULT 0,
  cache_read_tokens  INT DEFAULT 0,
  cache_creation_tokens INT DEFAULT 0,
  llm_requests       INT DEFAULT 0,
  is_error           BOOLEAN DEFAULT FALSE,
  ticket_id          TEXT NOT NULL DEFAULT 'unattributed',
  repo               TEXT,
  team               TEXT,
  user_hash          TEXT
);
CREATE INDEX IF NOT EXISTS ix_inv_ticket ON skill_invocation (ticket_id);
CREATE INDEX IF NOT EXISTS ix_inv_skill  ON skill_invocation (skill_name, skill_version, harness);

CREATE TABLE IF NOT EXISTS session_cost (           -- harness-native token metrics rolled up per session
  session_id TEXT PRIMARY KEY, harness TEXT, model TEXT, ticket_id TEXT, team TEXT,
  input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT, cache_creation_tokens BIGINT,
  cost_usd NUMERIC(12,4), active_seconds INT, started_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ticket (                  -- from Linear
  ticket_id TEXT PRIMARY KEY, team TEXT, story_points NUMERIC, created_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ, done_at TIMESTAMPTZ, cycle_time_hours NUMERIC, lead_time_hours NUMERIC,
  harness_arm TEXT                                   -- crossover assignment: claude-code | copilot | control
);

CREATE TABLE IF NOT EXISTS pull_request (            -- from GitHub
  pr_id TEXT PRIMARY KEY, repo TEXT, ticket_id TEXT, opened_at TIMESTAMPTZ, merged_at TIMESTAMPTZ,
  review_rounds INT, hours_to_first_approval NUMERIC, ci_failures INT,
  assisted_by TEXT                                   -- claude-code | copilot | none (from harness labels)
);

CREATE TABLE IF NOT EXISTS policy_result (           -- OPA/Rego outcome per PR per policy (CI artefact)
  pr_id TEXT, policy_id TEXT, run_seq INT,           -- run_seq 1 = first CI run on the PR
  passed BOOLEAN, evaluated_at TIMESTAMPTZ,
  PRIMARY KEY (pr_id, policy_id, run_seq)
);

CREATE TABLE IF NOT EXISTS defect (                  -- linked defects/incidents within window
  defect_id TEXT PRIMARY KEY, ticket_id TEXT, opened_at TIMESTAMPTZ, severity TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS skill_eval (              -- offline evals (design §4.6)
  eval_id TEXT PRIMARY KEY, skill_name TEXT, skill_version TEXT, harness TEXT, task_id TEXT,
  run_at TIMESTAMPTZ, passed BOOLEAN, total_tokens INT, duration_seconds NUMERIC, catalogue_commit TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_invocation_plugin ON skill_invocation (plugin);
