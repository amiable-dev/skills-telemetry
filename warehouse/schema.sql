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
  branch_hash        TEXT,                   -- ADR-013 join key; NULL = no repo or branch readable
  repo               TEXT,
  team               TEXT,
  user_hash          TEXT
);
-- Migrations for warehouses created before a column existed. CREATE TABLE IF NOT
-- EXISTS does nothing to a table that is already there, so a new column reaches
-- an existing database only here — and without it the loader fails on its first
-- INSERT after an upgrade, which is the worst possible moment to find out.
ALTER TABLE skill_invocation ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS ix_inv_skill  ON skill_invocation (skill_name, skill_version, harness);

CREATE TABLE IF NOT EXISTS session_cost (           -- harness-native token metrics rolled up per session
  session_id TEXT PRIMARY KEY, harness TEXT, model TEXT, branch_hash TEXT, team TEXT,
  input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT, cache_creation_tokens BIGINT,
  cost_usd NUMERIC(12,4), active_seconds INT, started_at TIMESTAMPTZ,
  api_ms BIGINT, tool_ms BIGINT, duration_ms BIGINT   -- harness wall time, cumulative per session
);

CREATE TABLE IF NOT EXISTS ticket (                  -- from Linear
  ticket_id TEXT PRIMARY KEY, team TEXT, story_points NUMERIC, created_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ, done_at TIMESTAMPTZ, cycle_time_hours NUMERIC, lead_time_hours NUMERIC,
  harness_arm TEXT                                   -- crossover assignment: claude-code | copilot | control
);

-- ADR-013: a pull request on GitHub, a merge request on GitLab. Filled by a
-- per-forge adapter; every query reads only this shape.
CREATE TABLE IF NOT EXISTS change_request (
  cr_id TEXT PRIMARY KEY,                            -- forge:host/owner/name!number
  forge TEXT NOT NULL, repo_id TEXT NOT NULL, number INT NOT NULL,
  source_branch TEXT,                                -- readable here; spans carry only the hash
  branch_hash TEXT,                                  -- the join key, as the capture side computes it
  opened_at TIMESTAMPTZ, merged_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
  state TEXT NOT NULL,                               -- merged | closed | open
  review_rounds INT, hours_to_first_approval NUMERIC, ci_failures INT,
  assisted_by TEXT                                   -- claude-code | copilot | mixed | none | unknown
);
CREATE INDEX IF NOT EXISTS ix_cr_branch ON change_request (branch_hash, closed_at);

-- Commit patch-ids per change request: the evidence side of the join.
CREATE TABLE IF NOT EXISTS change_request_commit (
  cr_id TEXT NOT NULL, patch_id TEXT NOT NULL, sha TEXT,
  PRIMARY KEY (cr_id, patch_id)
);
CREATE INDEX IF NOT EXISTS ix_crc_patch ON change_request_commit (patch_id);

-- The ticket as enrichment: the forge's issue links first, then a key found in
-- title, body or branch. `source` says which, so a query can trust the forge.
CREATE TABLE IF NOT EXISTS change_request_ticket (
  cr_id TEXT NOT NULL, ticket_id TEXT NOT NULL, source TEXT NOT NULL,   -- forge | title | body | branch
  PRIMARY KEY (cr_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS policy_result (           -- OPA/Rego outcome per change request per policy (CI artefact)
  cr_id TEXT, policy_id TEXT, run_seq INT,           -- run_seq 1 = first CI run on the change request
  passed BOOLEAN, evaluated_at TIMESTAMPTZ,
  PRIMARY KEY (cr_id, policy_id, run_seq)
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
  external_system    TEXT,                   -- kind = external: the process that reported the spend
  external_operation TEXT,                   -- its own bounded verb, e.g. consult | verify
  external_cost_usd  NUMERIC(12,6),          -- what was OBSERVED: billed by a provider, or a
                                             -- self-hosted figure. NULL means the emitter did not
                                             -- report one, which is common and must never be read
                                             -- as zero. The only amount that reconciles to an invoice
  external_cost_source TEXT,                 -- #88: provider | local. NULL = the emitter did not say,
                                             -- which predates the label and is not `provider`
  external_cost_estimated_usd NUMERIC(12,6), -- #88: priced from a list, never billed. Kept apart so
                                             -- summing external_cost_usd still means something
  external_tool_use_id TEXT,                 -- ADR-012: joins to mcp_tool_call, never rewrites session_id
  external_requests_unpriced INT,            -- ADR-012: > 0 makes external_cost_usd a lower bound
  external_requests  INT,
  external_duration_ms BIGINT,
  agent_version      TEXT,                   -- ADR-011: what the agent's own file asserts
  agent_owner        TEXT,
  agent_content_hash TEXT,                   -- and the hash of what it actually says
  standard_id        TEXT,                   -- the contract, for whichever kind states one
  policy_ids         TEXT[],
  compaction_reason  TEXT,                   -- kind = compaction
  compaction_tokens_before BIGINT,           -- the harness's own estimates, recorded as received
  compaction_tokens_after  BIGINT,
  compaction_turns_since_previous INT,
  hook_ms            BIGINT,                 -- kind = turn: every hook that fired, summed
  hook_ms_by_hook    JSONB,                  -- {hook basename: ms}; basenames only, never paths
  scope_name         TEXT,                   -- ADR-010: the scoping artefact, stable for a run
  scope_key          TEXT,                   -- the unit instance (a branch hash), rolls each iteration
  scope_id           TEXT,                   -- unique per container instance; never a metrics label
  scope_source       TEXT,                   -- artefact | overlay: declared, or assumed for it
  branch_hash        TEXT,                   -- ADR-013 join key; NULL = no repo or branch readable
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
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS scope_name TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS scope_key TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS scope_id TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS scope_source TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS agent_version TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS agent_owner TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS agent_content_hash TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS standard_id TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS policy_ids TEXT[];
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_system TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_operation TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_cost_usd NUMERIC(12,6);
-- #88: provenance of the observed cost, and the estimate that travels beside it.
-- The estimate is a separate column, never folded into external_cost_usd, so a
-- sum over the observed column still reconciles against an invoice.
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_cost_source TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_cost_estimated_usd NUMERIC(12,6);
-- #88: what the loader saw and could not load. Tempo keeps an unknown attribute;
-- this is where it would otherwise vanish without a trace.
ALTER TABLE loader_run ADD COLUMN IF NOT EXISTS unknown_attrs INT;
ALTER TABLE loader_run ADD COLUMN IF NOT EXISTS unknown_attr_keys TEXT[];
-- ADR-012, contract v3. The join key an MCP emitter received for the call that
-- caused its run, and how many requests in that run carried no cost at all.
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_tool_use_id TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_requests_unpriced INT;
-- The other half of the join: every MCP call a turn or sub-agent recorded from
-- its transcript. External rows keep the session.id the emitter sent; queries
-- resolve the real session and turn through this table at read time.
CREATE TABLE IF NOT EXISTS mcp_tool_call (
  tool_use_id        TEXT PRIMARY KEY,
  session_id         TEXT NOT NULL,
  prompt_id          TEXT,
  activation_span_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_act_tool_use ON artefact_activation (external_tool_use_id);
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_requests INT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS external_duration_ms BIGINT;
CREATE INDEX IF NOT EXISTS artefact_activation_scope_idx
  ON artefact_activation (scope_name, scope_key) WHERE scope_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS artefact_activation_parent_idx
  ON artefact_activation (parent_span_id) WHERE parent_span_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_act_session ON artefact_activation (session_id, started_at);
CREATE INDEX IF NOT EXISTS ix_act_kind    ON artefact_activation (kind, started_at);
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
  error         TEXT,
  unknown_attrs INT,                         -- #88: attributes on external spans this loader
  unknown_attr_keys TEXT[]                   -- does not read. Kept by Tempo, lost here
);
CREATE INDEX IF NOT EXISTS ix_loader_run_recent ON loader_run (loader, started_at DESC);

-- ADR-013: the ticket leaves the capture tables. The join to delivery data is a
-- branch identity plus a time window, confirmed by commit evidence. Prior data
-- was dropped at the cutover (single user, 2026-09-30), so the old column goes
-- rather than being migrated; DROP COLUMN takes its index with it.
ALTER TABLE skill_invocation    ADD COLUMN IF NOT EXISTS branch_hash TEXT;
ALTER TABLE artefact_activation ADD COLUMN IF NOT EXISTS branch_hash TEXT;
ALTER TABLE session_cost        ADD COLUMN IF NOT EXISTS branch_hash TEXT;
ALTER TABLE skill_invocation    DROP COLUMN IF EXISTS ticket_id;
ALTER TABLE artefact_activation DROP COLUMN IF EXISTS ticket_id;
ALTER TABLE session_cost        DROP COLUMN IF EXISTS ticket_id;
CREATE INDEX IF NOT EXISTS ix_inv_branch ON skill_invocation (branch_hash, started_at);
CREATE INDEX IF NOT EXISTS ix_act_branch ON artefact_activation (branch_hash, started_at);

-- ADR-013: patch-ids of commits a session made, per turn. The evidence that
-- confirms a branch join and repairs a rename or a cherry-pick. Patch-ids, not
-- SHAs: a rebase keeps the diff and loses the SHA.
CREATE TABLE IF NOT EXISTS commit_evidence (
  patch_id           TEXT NOT NULL,
  activation_span_id TEXT NOT NULL,
  session_id         TEXT NOT NULL,
  prompt_id          TEXT,
  branch_hash        TEXT,
  observed_at        TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (patch_id, activation_span_id)
);
CREATE INDEX IF NOT EXISTS ix_evidence_patch ON commit_evidence (patch_id);

-- ADR-013 cutover for a warehouse created before it: pull_request is replaced by
-- change_request, and policy_result is keyed on cr_id. Prior delivery data is
-- dropped rather than migrated (single user, confirmed); a policy_result still
-- keyed on pr_id is recreated empty.
DROP TABLE IF EXISTS pull_request;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'policy_result' AND column_name = 'pr_id') THEN
    DROP TABLE policy_result;
    CREATE TABLE policy_result (cr_id TEXT, policy_id TEXT, run_seq INT, passed BOOLEAN,
                                evaluated_at TIMESTAMPTZ, PRIMARY KEY (cr_id, policy_id, run_seq));
  END IF;
END $$;

-- ADR-013 decision 4: which change request each activation's work became, and
-- how that was established. Read-time, so arrival order never matters, and the
-- only place attribution is defined — the scorecard and every panel read this.
--
--   branch         same branch hash; the change request with the earliest close
--                  at or after the activation, else the one still open. Bounded
--                  to 30 days before the change request opened, so a reused
--                  branch name (`fix/typo`) cannot claim year-old activity.
--   branch+commit  as above, confirmed: a patch-id recorded on that branch is in
--                  that change request.
--   commit         no branch match, but this session's own patch-ids landed in a
--                  change request: a branch renamed before its PR opened, or a
--                  cherry-pick onto another branch.
CREATE OR REPLACE VIEW activation_change_request AS
WITH by_branch AS (
  SELECT DISTINCT ON (a.span_id) a.span_id, a.branch_hash, c.cr_id
  FROM artefact_activation a
  JOIN change_request c
    ON c.branch_hash = a.branch_hash
   AND (c.closed_at IS NULL OR c.closed_at >= a.started_at)
   AND a.started_at >= c.opened_at - interval '30 days'
  WHERE a.branch_hash IS NOT NULL
  ORDER BY a.span_id, c.closed_at ASC NULLS LAST, c.opened_at ASC
),
confirmed AS (
  SELECT DISTINCT e.branch_hash, cc.cr_id
  FROM commit_evidence e
  JOIN change_request_commit cc ON cc.patch_id = e.patch_id
),
by_commit AS (
  SELECT DISTINCT ON (a.span_id) a.span_id, c.cr_id
  FROM artefact_activation a
  JOIN commit_evidence e ON e.session_id = a.session_id
                        AND e.branch_hash IS NOT DISTINCT FROM a.branch_hash
  JOIN change_request_commit cc ON cc.patch_id = e.patch_id
  JOIN change_request c ON c.cr_id = cc.cr_id
  WHERE (c.closed_at IS NULL OR c.closed_at >= a.started_at)
    AND NOT EXISTS (SELECT 1 FROM by_branch b WHERE b.span_id = a.span_id)
  ORDER BY a.span_id, c.closed_at ASC NULLS LAST
)
SELECT b.span_id, b.cr_id,
       CASE WHEN EXISTS (SELECT 1 FROM confirmed f
                         WHERE f.branch_hash = b.branch_hash AND f.cr_id = b.cr_id)
            THEN 'branch+commit' ELSE 'branch' END AS method
FROM by_branch b
UNION ALL
SELECT span_id, cr_id, 'commit' FROM by_commit;

-- ADR-014 decision 5: one row per model request, from the harness's own record.
-- Claude Code's come from its `api_request` events in Loki; Copilot's will come
-- from its `chat` spans (#115). Authoritative per request: per-skill cost is a
-- sum over this table. cost_usd is unbounded NUMERIC because the harness sends
-- seven decimals and more, and NULL where no cost exists (Copilot) — never 0.
CREATE TABLE IF NOT EXISTS llm_request (
  harness            TEXT NOT NULL,          -- claude-code | copilot-*
  request_id         TEXT NOT NULL,          -- the provider's id (Claude Code `request_id`)
  session_id         TEXT,
  prompt_id          TEXT,                   -- Claude Code: the bridge to the stdtel turn
  conversation_id    TEXT,                   -- Copilot: gen_ai.conversation.id
  trace_id           TEXT,                   -- Copilot: the invoke_agent trace
  ended_at           TIMESTAMPTZ NOT NULL,   -- when the harness logged the completed request
  duration_ms        BIGINT,
  model              TEXT,
  input_tokens          BIGINT,
  output_tokens         BIGINT,
  cache_read_tokens     BIGINT,
  cache_creation_tokens BIGINT,
  cost_usd           NUMERIC,
  skill_name         TEXT,                   -- "third-party" when the harness redacts it (#117)
  agent_name         TEXT,
  plugin_name        TEXT,
  mcp_server         TEXT,
  mcp_tool           TEXT,
  query_source       TEXT,                   -- main | subagent | auxiliary | sdk
  branch_hash        TEXT,                   -- Copilot only; Claude Code joins through prompt_id
  user_hash          TEXT,                   -- std.user.hash, set by the collector
  attribution_source TEXT NOT NULL CHECK (attribution_source IN ('native', 'derived')),
  PRIMARY KEY (harness, request_id)
);
CREATE INDEX IF NOT EXISTS ix_req_prompt ON llm_request (session_id, prompt_id);
CREATE INDEX IF NOT EXISTS ix_req_time   ON llm_request (ended_at);

-- ADR-014 decision 7: which change request each request's work became. A request
-- carries its prompt id; the stdtel turn with that prompt id carries the branch,
-- and activation_change_request — the only place ADR-013's rule is written —
-- does the rest. Only turns: a sub-agent shares its parent's prompt id, and a
-- prompt has several turn deltas (ADR-009), so without the restriction and the
-- DISTINCT ON each request would be counted once per matching span (#23).
CREATE OR REPLACE VIEW llm_request_change_request AS
SELECT DISTINCT ON (r.harness, r.request_id)
       r.harness, r.request_id, v.cr_id, v.method
FROM llm_request r
JOIN artefact_activation t ON t.kind = 'turn'
                          AND t.session_id = r.session_id
                          AND t.prompt_id = r.prompt_id
JOIN activation_change_request v ON v.span_id = t.span_id
ORDER BY r.harness, r.request_id,
         CASE v.method WHEN 'branch+commit' THEN 0 WHEN 'commit' THEN 1 ELSE 2 END,
         t.started_at;

-- ADR-014 decision 5: session_cost is the harness's own cumulative total, kept
-- only to check the sum of requests against. The two are never added together.
CREATE OR REPLACE VIEW session_cost_reconciliation AS
SELECT s.session_id, s.harness,
       s.cost_usd                  AS session_total_usd,
       sum(r.cost_usd)             AS requests_usd,
       count(r.request_id)::int    AS requests,
       s.cost_usd - sum(r.cost_usd) AS unexplained_usd
FROM session_cost s
LEFT JOIN llm_request r ON r.session_id = s.session_id AND r.harness = s.harness
GROUP BY s.session_id, s.harness, s.cost_usd;

-- #122: the capture side sends std.branch.hash = "" when a session has no
-- repository, and the loader stored it as ''. Missing is NULL (ADR-005). Touches
-- only rows holding the empty string; idempotent.
UPDATE artefact_activation SET branch_hash = NULL WHERE branch_hash = '';
UPDATE skill_invocation    SET branch_hash = NULL WHERE branch_hash = '';
UPDATE session_cost        SET branch_hash = NULL WHERE branch_hash = '';
UPDATE commit_evidence     SET branch_hash = NULL WHERE branch_hash = '';
