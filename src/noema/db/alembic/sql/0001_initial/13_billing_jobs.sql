-- 0001_initial / 13: billing, idempotency, audit, jobs and the outbox dead letters
-- (spec-03 §2; IMPL-019, IMPL-020, IMPL-022, IMPL-077, IMPL-085; spec-11 §3-§5).
CREATE TABLE noema.usage_line (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL,
  line text NOT NULL CHECK (line IN ('decision','generation','labeling','verification','waste','cache_savings')),
  model_id text, region text,                                                  -- Bedrock id; NULL only for cache_savings
  session_id text, turn_id text, decision_id text, attempt_id text, labeling_run_id text,
  input_tokens int NOT NULL, output_tokens int NOT NULL, cached_input_tokens int NOT NULL,
  cache_write_input_tokens int NOT NULL, reasoning_tokens int NOT NULL, tokens_estimated bool NOT NULL,
  cost_usd numeric(18,6) NOT NULL, cost_estimated bool NOT NULL,
  at timestamptz NOT NULL, day date GENERATED ALWAYS AS ((at AT TIME ZONE 'UTC')::date) STORED,
  PRIMARY KEY (tenant_id, id),                                                 -- id = idempotent upsert key (outbox)
  CHECK (line = 'cache_savings' OR (model_id IS NOT NULL AND region IS NOT NULL)));
CREATE INDEX usage_report ON noema.usage_line (tenant_id, project_id, day, line);   -- developer (project) reports
CREATE INDEX usage_report_tenant ON noema.usage_line (tenant_id, day);              -- tenant_admin/auditor reports

CREATE TABLE noema.idempotency_record (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL,
  scope_hash text NOT NULL, key text NOT NULL,
  fingerprint text NOT NULL, state text NOT NULL CHECK (state IN ('in_flight','completed','not_stored')),
  owner text NOT NULL, lease_expires_at timestamptz NOT NULL, resource_id text,
  response_status int, response_headers jsonb, response_body bytea, sse_turn_id text,   -- inline body <= 1 MiB
  response_body_key text,                     -- larger body: S3 key '<tenant_id>/idempotency/<id>' (IMPL-085)
  CHECK (response_body IS NULL OR response_body_key IS NULL),
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, project_id, scope_hash, key));   -- the unique index serves begin()
CREATE INDEX idempotency_expiry ON noema.idempotency_record (expires_at);

CREATE TABLE noema.audit_event (
  tenant_id text NOT NULL CHECK (tenant_id <> ''),                             -- '__platform__' for platform events
  id text NOT NULL, actor_type text NOT NULL CHECK (actor_type IN ('principal','operator','system')),
  actor_id text NOT NULL, action text NOT NULL, resource_kind text NOT NULL, resource_id text,
  details jsonb NOT NULL DEFAULT '{}',                                         -- never content
  request_id text, at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, id));

CREATE TABLE jobs.job (                                                       -- tenant_id '__platform__': periodic jobs
  id text PRIMARY KEY, tenant_id text NOT NULL CHECK (tenant_id <> ''), kind text NOT NULL,
  priority int NOT NULL DEFAULT 100,                                           -- lower = first
  payload jsonb NOT NULL, state text NOT NULL CHECK (state IN ('queued','running','done','dead')),
  attempts int NOT NULL DEFAULT 0, max_attempts int NOT NULL DEFAULT 5, run_after timestamptz NOT NULL DEFAULT now(),
  locked_by text, locked_until timestamptz, dedupe_key text,
  last_error text,                                                             -- code + <= 200 chars, redacted
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'running') = (locked_by IS NOT NULL AND locked_until IS NOT NULL)));
CREATE INDEX job_ready ON jobs.job (priority, run_after) WHERE state = 'queued';          -- jobs.claim
CREATE INDEX job_running ON jobs.job (locked_until) WHERE state = 'running';               -- ops.reap_stuck
CREATE INDEX job_finished ON jobs.job (updated_at) WHERE state IN ('done','dead');          -- old-job cleanup
CREATE UNIQUE INDEX job_dedupe ON jobs.job (tenant_id, kind, dedupe_key)
  WHERE dedupe_key IS NOT NULL AND state IN ('queued','running');
-- enqueue: INSERT ... ON CONFLICT (tenant_id, kind, dedupe_key) WHERE dedupe_key IS NOT NULL AND state IN
--          ('queued','running') DO NOTHING RETURNING id; no row -> SELECT the live duplicate's id

CREATE TABLE ops.outbox_dead_letter (
  kind text NOT NULL, id text NOT NULL, tenant_id text NOT NULL CHECK (tenant_id <> ''), payload jsonb NOT NULL,
  error text NOT NULL, attempts int NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, id));             -- a labeling_offer and a decision_row share the decision id; definer-only
