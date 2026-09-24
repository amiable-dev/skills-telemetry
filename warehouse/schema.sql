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
-- Migrations for warehouses created before a column existed. CREATE TABLE IF NOT
-- EXISTS does nothing to a table that is already there, so a new column reaches
-- an existing database only here — and without it the loader fails on its first
-- INSERT after an upgrade, which is the worst possible moment to find out.
ALTER TABLE skill_invocation ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS ix_inv_ticket ON skill_invocation (ticket_id);
CREATE INDEX IF NOT EXISTS ix_inv_skill  ON skill_invocation (skill_name, skill_version, harness);

CREATE TABLE IF NOT EXISTS session_cost (           -- harness-native token metrics rolled up per session
  session_id TEXT PRIMARY KEY, harness TEXT, model TEXT, ticket_id TEXT, team TEXT,
  input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT, cache_creation_tokens BIGINT,
  cost_usd NUMERIC(12,4), active_seconds INT, started_at TIMESTAMPTZ,
  api_ms BIGINT, tool_ms BIGINT, duration_ms BIGINT   -- harness wall time, cumulative per session
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

-- ADR-009: the unit of capture is an artefact activation, not a skill invocation.
-- One span name (std.artefact.activation) discriminated by kind, so "where did
-- the tokens go" is a GROUP BY rather than a union over four tables. The token
-- columns carry NO DEFAULT on purpose: a missing attribute must arrive as NULL,
-- because 0 is a measurement ("this subagent read no cached tokens") and NULL is
-- the absence of one (ADR-005). skill_invocation keeps its DEFAULT 0 because
-- rows written before ADR-009 were loaded that way and changing it would make
-- old and new rows mean different things.
CREATE TABLE IF NOT EXISTS artefact_activation (
  span_id            TEXT PRIMARY KEY,
  trace_id           TEXT NOT NULL,
  parent_span_id     TEXT,                   -- ADR-010: the turn this ran under. NULL is a real
                                             -- observation, not a gap: a compaction has no turn,
                                             -- and so does every row loaded before #75, when every
                                             -- span was the root of its own trace
  session_id         TEXT NOT NULL,
  started_at         TIMESTAMPTZ NOT NULL,
  ended_at           TIMESTAMPTZ NOT NULL,
  kind               TEXT NOT NULL,          -- skill | subagent | compaction | turn
  name               TEXT,                   -- catalogue name / agent type / compaction reason;
                                             -- NULL on a turn, whose identity is prompt_id and
                                             -- which must never contribute a metrics dimension
  source             TEXT,                   -- hook | transcript: observed, or inferred (ADR-005)
  harness            TEXT,                   -- claude-code | copilot-vscode | copilot-cli
  harness_mode       TEXT,
  prompt_id          TEXT,                   -- join key to native claude_code.* telemetry
  parent_prompt_id   TEXT,                   -- the turn that spawned a sub-agent
  model              TEXT,
  input_tokens          BIGINT,
  output_tokens         BIGINT,
  cache_read_tokens     BIGINT,
  cache_creation_tokens BIGINT,
  llm_requests       INT,
  tool_calls         INT,
  duration_ms        BIGINT,
  is_error           BOOLEAN,
  subagent_type      TEXT,                   -- kind = subagent
  subagent_id        TEXT,
  subagent_depth     INT,
  compaction_reason  TEXT,                   -- kind = compaction
  compaction_tokens_before BIGINT,           -- the harness's own estimates, recorded as received
  compaction_tokens_after  BIGINT,
  compaction_turns_since_previous INT,
  hook_ms            BIGINT,                 -- kind = turn: every hook that fired, summed
  hook_ms_by_hook    JSONB,                  -- {hook basename: ms}; basenames only, never paths
  ticket_id          TEXT NOT NULL DEFAULT 'unattributed',
  repo               TEXT,
  team               TEXT,
  user_hash          TEXT
);
-- Migrations. A CREATE TABLE guarded by IF NOT
-- EXISTS does nothing to a table that is already there, so every column added
-- after a table's first release reaches an existing warehouse only through an
-- ALTER; without one the loader discovers the gap on its first INSERT after an
-- upgrade (#55).
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS hook_ms_by_hook JSONB;
-- ADR-010: containment. Emitted since #75 — before that every span was the root
-- of its own trace, so this is NULL for every row loaded earlier and that is a
-- real distinction, not a gap to backfill: those spans genuinely had no parent.
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
CREATE INDEX IF NOT EXISTS artefact_activation_parent_idx
  ON artefact_activation (parent_span_id) WHERE parent_span_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_act_session ON artefact_activation (session_id, started_at);
CREATE INDEX IF NOT EXISTS ix_act_kind    ON artefact_activation (kind, started_at);
CREATE INDEX IF NOT EXISTS ix_act_ticket  ON artefact_activation (ticket_id);
CREATE INDEX IF NOT EXISTS ix_act_prompt  ON artefact_activation (session_id, prompt_id);

-- Real money and real wall time, from the harness's own cost-state entry. These
-- are cumulative for the session, which is why they are here and not on a turn.
ALTER TABLE session_cost ADD COLUMN IF NOT EXISTS api_ms BIGINT;
ALTER TABLE session_cost ADD COLUMN IF NOT EXISTS tool_ms BIGINT;
ALTER TABLE session_cost ADD COLUMN IF NOT EXISTS duration_ms BIGINT;

-- ADR-009 decision 7: the loaders run on a schedule, and a warehouse that lags
-- Tempo is a finding rather than a surprise. A run that failed writes a row too;
-- a loader that only records its successes cannot be distinguished from one that
-- was never run at all.
CREATE TABLE IF NOT EXISTS loader_run (
  run_id        TEXT PRIMARY KEY,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ,
  loader        TEXT NOT NULL,               -- load_traces | load_delivery
  rows_loaded   INT,
  source_max_ts TIMESTAMPTZ,                 -- newest source timestamp seen: the lag measurement
  ok            BOOLEAN,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_loader_run_recent ON loader_run (loader, started_at DESC);
