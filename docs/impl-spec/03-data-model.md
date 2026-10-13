# 03 — Data model, DDL and RLS (Postgres 16)

Wave A, lead architect. The db role (B2) turns this file into the first alembic migration; foundation then freezes it. Resolutions: IMPL-009, IMPL-010, IMPL-022, IMPL-071, IMPL-072, IMPL-077 ([`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md)).

## 1. Roles and schemas

| Role | Login | Attributes | Privileges |
|---|---|---|---|
| `noema_owner` | yes (migrations only) | NOSUPERUSER, NOBYPASSRLS; member of `noema_definer` (`WITH INHERIT FALSE, SET TRUE`, so it can hand functions to it) | owns every schema, table and sequence; never used by the app |
| `noema_app` | yes (API, workers) | NOSUPERUSER, **NOBYPASSRLS**, no CREATE on any schema | the grant table below; EXECUTE only on the §4 functions granted to it |
| `noema_definer` | **no** | BYPASSRLS | owns every SECURITY DEFINER function in §4; exactly the table privileges those functions use |
| `noema_cli` | yes (`noema-admin`) | NOBYPASSRLS | EXECUTE on `admin.*` and `auth.lookup_operator`; no table privileges |

`noema_app` grants (USAGE on the schemas `noema`, `jobs`, `platform`; nothing on `auth`, `ops`, `admin` except EXECUTE on its §4 functions):

| Object | Privileges |
|---|---|
| `noema.tenant`, `noema.project`, `noema.principal`, `noema.calibrator_epoch` | SELECT |
| `noema.session`, `turn`, `turn_file`, `attempt`, `decision`, `file`, `derivative`, `idempotency_record`, `labeling_run`, `label_item` | SELECT, INSERT, UPDATE, DELETE |
| `noema.calibrator` | SELECT, INSERT, UPDATE (tenant rows only, by RLS) |
| `noema.usage_line` | SELECT (rows are written by `ops.outbox_apply`) |
| `noema.audit_event` | SELECT, INSERT (append-only) |
| `jobs.job` | SELECT, INSERT (claim, finish and extend go through §4) |
| `platform.config_version`, `platform.price_catalog` | SELECT |
| `auth.api_key`, `platform.operator`, `ops.outbox_dead_letter` | none |

Schemas: `noema` (tenant data), `auth` (keys), `jobs`, `ops`, `admin` (CLI functions), `platform` (operator- and config-level rows; no tenant data). The migration runs `REVOKE CREATE ON SCHEMA public FROM PUBLIC` and `REVOKE ALL ON DATABASE <db> FROM PUBLIC`, then grants CONNECT to the three login roles.

Conventions:
- Ids are `text`: a prefixed ULID (spec-02 §1). `tenant_id` is `text`, the config tenant key (`t_acme`); `CHECK (tenant_id <> '')` on every tenant table, so an empty `app.tenant_id` never matches a row.
- Times are `timestamptz`.
- JSON is `jsonb`.
- Money is `numeric(18,6)`.
- Every tenant table has `tenant_id text NOT NULL`, composite primary keys or foreign keys starting with `tenant_id`, and the RLS of §3. Exceptions, each with a global key by design: `auth.api_key` (`key_id`, IMPL-008), `jobs.job` (`id`), `noema.calibrator` (`id`; `tenant_id` is NULL for platform rows) and `noema.calibrator_epoch` (`scope_key`).
- List endpoints page by keyset on the stated order (spec-02 §7); ULID ids sort by creation time, so "newest first" is `id DESC`.

## 2. Tables

```sql
-- platform (no RLS; noema_app has SELECT on config_version and price_catalog only, §1)
CREATE TABLE platform.config_version (
  version int PRIMARY KEY, digest text NOT NULL, body jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('proposed','active','superseded','rejected')),
  proposed_by text NOT NULL, proposed_at timestamptz NOT NULL DEFAULT now(),
  approved_by text, approved_at timestamptz, activated_at timestamptz,
  CHECK (approved_by IS NULL OR approved_by <> proposed_by));                    -- two-person (IMPL-044)
CREATE UNIQUE INDEX config_one_active ON platform.config_version ((true)) WHERE status = 'active';
CREATE TABLE platform.price_catalog (                                          -- spec-05 §2; referenced as prices_ref "db:<version>"
  version int PRIMARY KEY, digest text NOT NULL, body jsonb NOT NULL,          -- body = prices.yaml as JSON, schema-validated
  imported_by text NOT NULL, imported_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE platform.operator (
  id text PRIMARY KEY, name text UNIQUE NOT NULL, key_id text UNIQUE NOT NULL, key_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz);

-- tenancy and identity
CREATE TABLE noema.tenant (
  id text PRIMARY KEY, aws_account_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz);       -- synced from config on activation
CREATE TABLE noema.project (
  tenant_id text NOT NULL REFERENCES noema.tenant(id), id text NOT NULL, name text NOT NULL,
  default_data_class text NOT NULL DEFAULT 'internal' CHECK (default_data_class IN ('public','internal','confidential','restricted')),
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, name));
CREATE TABLE noema.principal (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, subject text NOT NULL,
  roles text[] NOT NULL CHECK (roles <@ ARRAY['tenant_admin','developer','reviewer','end_user','auditor']),
  created_at timestamptz NOT NULL DEFAULT now(), disabled_at timestamptz,
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, project_id) REFERENCES noema.project, UNIQUE (tenant_id, project_id, subject));
CREATE TABLE auth.api_key (
  key_id text PRIMARY KEY,                                                     -- globally unique ULID (IMPL-008)
  tenant_id text NOT NULL, project_id text NOT NULL, principal_id text NOT NULL,
  key_hash text NOT NULL, pepper_version int NOT NULL, env text NOT NULL CHECK (env IN ('live','test')),
  scopes text[] NOT NULL CHECK (scopes <@ ARRAY['turns','decisions','sessions','files.read','files.write','usage.read','admin.read']),
  ip_allowlist cidr[], expires_at timestamptz, revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz,
  FOREIGN KEY (tenant_id, principal_id) REFERENCES noema.principal);

-- sessions and turns
CREATE TABLE noema.session (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, owner_principal_id text NOT NULL,
  data_class text NOT NULL CHECK (data_class IN ('public','internal','confidential','restricted')),
  retention text NOT NULL DEFAULT 'default' CHECK (retention IN ('default','ephemeral')),   -- custom: 422 in Phase 1
  expires_at timestamptz, metadata jsonb NOT NULL DEFAULT '{}',
  last_seq int NOT NULL DEFAULT 0, sticky_spec jsonb,                         -- IMPL-014; AttemptSpec fields as JSON
  active_turn_id text, lease_expires_at timestamptz,                           -- session ordering lease (IMPL-022)
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, project_id) REFERENCES noema.project,
  CHECK ((active_turn_id IS NULL) = (lease_expires_at IS NULL)));
CREATE INDEX session_by_project ON noema.session (tenant_id, project_id, id DESC) WHERE deleted_at IS NULL;   -- listSessions (admin)
CREATE INDEX session_by_owner ON noema.session (tenant_id, owner_principal_id, id DESC) WHERE deleted_at IS NULL;  -- listSessions
CREATE INDEX session_expiry ON noema.session (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE noema.turn (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, session_id text, seq int,
  principal_id text NOT NULL, status text NOT NULL CHECK (status IN ('completed','failed','cancelled','abandoned')),
  content_policy text NOT NULL CHECK (content_policy IN ('full','redacted','metadata_only')),
  input_parts jsonb, output_text text,                                         -- per content_policy (IMPL-018)
  redactions jsonb NOT NULL DEFAULT '{"user": [], "assistant": []}',           -- SessionTurn.redactions per role (code points)
  generation_profile text, routing jsonb NOT NULL, usage jsonb NOT NULL, timings jsonb NOT NULL,
  degradations jsonb NOT NULL DEFAULT '[]', decision_id text, verification_decision_id text, retry_of text,  -- retry_of unused in P1
  created_at timestamptz NOT NULL, completed_at timestamptz NOT NULL, expires_at timestamptz,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, session_id, seq),            -- the unique index serves history by seq
  FOREIGN KEY (tenant_id, session_id) REFERENCES noema.session, CHECK ((session_id IS NULL) = (seq IS NULL)));
CREATE INDEX turn_expiry ON noema.turn (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE noema.turn_file (
  tenant_id text NOT NULL, turn_id text NOT NULL, part_index int NOT NULL, file_id text NOT NULL,
  PRIMARY KEY (tenant_id, turn_id, part_index), FOREIGN KEY (tenant_id, turn_id) REFERENCES noema.turn ON DELETE CASCADE);
CREATE TABLE noema.attempt (
  tenant_id text NOT NULL, id text NOT NULL, turn_id text NOT NULL, route_id text NOT NULL,
  model text NOT NULL,                                                          -- Bedrock id (TurnObject.attempts[].model)
  spec_signature text NOT NULL, outcome text NOT NULL CHECK (outcome IN ('committed','cancelled','failed')),   -- IMPL-011
  cancel_reason text CHECK (cancel_reason IN ('spec_changed','lost_race','client_abort','failed','deadline')),
  finish_reason text CHECK (finish_reason IN ('stop','length','content_filter','cancelled')), error_code text, usage jsonb NOT NULL,
  cost_usd numeric(18,6) NOT NULL, cost_estimated bool NOT NULL, started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, turn_id) REFERENCES noema.turn ON DELETE CASCADE);
CREATE INDEX attempt_by_turn ON noema.attempt (tenant_id, turn_id, started_at);

-- decisions (IMPL-071); verification results are decision rows with purpose = 'verification'
CREATE TABLE noema.decision (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, created_at timestamptz NOT NULL,
  principal_id text NOT NULL,                                                  -- the caller; getDecision "own" for end_user (spec-09 §3)
  turn_id text, session_id text, purpose text NOT NULL CHECK (purpose IN ('decision','router','verification')),
  origin text NOT NULL CHECK (origin IN ('native','facade','turn','verification')),
  output_format text NOT NULL CHECK (output_format IN ('native','jev','openai')),
  profile_id text NOT NULL, profile_version text NOT NULL, question_signature text NOT NULL,
  input_view_signature text NOT NULL, input_digest text NOT NULL,
  status text NOT NULL CHECK (status IN ('completed','partial')), cache_hit bool NOT NULL, safety_identifier text,
  questions jsonb NOT NULL,                                                    -- compiled wire dicts, answer order
  input jsonb,                                                                 -- NULL when content_policy = metadata_only
  result jsonb NOT NULL, expires_at timestamptz,
  PRIMARY KEY (tenant_id, id));
CREATE INDEX decision_by_session ON noema.decision (tenant_id, session_id) WHERE session_id IS NOT NULL;   -- session.delete
CREATE INDEX decision_expiry ON noema.decision (expires_at) WHERE expires_at IS NOT NULL;

-- files
CREATE TABLE noema.file (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, principal_id text NOT NULL, session_id text,
  origin text NOT NULL CHECK (origin IN ('upload','inline')), filename_display text,   -- display only; never sent to models
  declared_type text NOT NULL, media_type text, bytes bigint NOT NULL, sha256 text NOT NULL,   -- media_type NULL until detect;
                                                                               -- FileObject.media_type falls back to declared_type
  status text NOT NULL CHECK (status IN ('quarantined','processing','ready','partial','rejected','failed','deleted')),
  rejection jsonb, failure jsonb, quarantine_key text NOT NULL, pages int,
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz, expires_at timestamptz,
  PRIMARY KEY (tenant_id, id));
CREATE INDEX file_by_session ON noema.file (tenant_id, session_id) WHERE session_id IS NOT NULL;   -- session.delete
CREATE INDEX file_expiry ON noema.file (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE noema.derivative (                                                -- raster_1568 is internal (spec-02 §6)
  tenant_id text NOT NULL, id text NOT NULL, file_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('raster','raster_1568','thumbnail','ocr_text','pdf_sanitized','markdown','page_images','text')),
  status text NOT NULL CHECK (status IN ('pending','ready','failed')), extractor_version text NOT NULL,
  sha256 text, bytes bigint, pages int, width int, height int,                 -- width/height: single-image kinds (manifest)
  text_layer_pages int[] CHECK (text_layer_pages IS NULL OR (kind = 'page_images' AND 0 < ALL (text_layer_pages))),
                                                                               -- page_images: 1-based pages with a text layer (ADR-002 §5)
  facts jsonb CONSTRAINT derivative_facts_closed CHECK (facts IS NULL OR kind = 'pdf_sanitized' AND <exactly
    {"active_content": [...]}, values of the spec-09 §6.3 table, [] allowed>),   -- rev 0002 (C2a); outlives the sandbox run
  storage_key text, error_code text,
  created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, file_id, kind),
  FOREIGN KEY (tenant_id, file_id) REFERENCES noema.file ON DELETE CASCADE);
-- S3 layout: s3://noema-<env>/<tenant_id>/quarantine/<file_id>; .../derivatives/<file_id>/<kind>[/page-NNNN.png];
--            .../idempotency/<idempotency_record.id> (response bodies > 1 MiB, IMPL-085)

-- billing, idempotency, audit (IMPL-019, IMPL-020, IMPL-022, IMPL-077, IMPL-085)
CREATE TABLE noema.usage_line (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL,
  line text NOT NULL CHECK (line IN ('decision','generation','labeling','verification','waste','cache_savings')),
  model_id text, region text,                                                  -- Bedrock id; NULL only for cache_savings
  session_id text, turn_id text, decision_id text, attempt_id text, labeling_run_id text,
  input_tokens int NOT NULL, output_tokens int NOT NULL, cached_input_tokens int NOT NULL, cache_write_input_tokens int NOT NULL,
  reasoning_tokens int NOT NULL, tokens_estimated bool NOT NULL, cost_usd numeric(18,6) NOT NULL, cost_estimated bool NOT NULL,
  at timestamptz NOT NULL, day date GENERATED ALWAYS AS ((at AT TIME ZONE 'UTC')::date) STORED,
  PRIMARY KEY (tenant_id, id),                                                 -- id = idempotent upsert key from the outbox
  CHECK (line = 'cache_savings' OR (model_id IS NOT NULL AND region IS NOT NULL)));
CREATE INDEX usage_report ON noema.usage_line (tenant_id, project_id, day, line);   -- developer (project) reports
CREATE INDEX usage_report_tenant ON noema.usage_line (tenant_id, day);              -- tenant_admin/auditor (tenant-wide)
CREATE TABLE noema.idempotency_record (
  tenant_id text NOT NULL, project_id text NOT NULL, id text NOT NULL, scope_hash text NOT NULL, key text NOT NULL,
  fingerprint text NOT NULL, state text NOT NULL CHECK (state IN ('in_flight','completed','not_stored')),
  owner text NOT NULL, lease_expires_at timestamptz NOT NULL, resource_id text,
  response_status int, response_headers jsonb, response_body bytea, sse_turn_id text,   -- inline body ≤ 1 MiB
  response_body_key text,           -- larger body: S3 key '<tenant_id>/idempotency/<id>' (IMPL-085); same TTL as the row
  CHECK (response_body IS NULL OR response_body_key IS NULL),
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, project_id, scope_hash, key));   -- the unique index serves begin()
CREATE INDEX idempotency_expiry ON noema.idempotency_record (expires_at);
CREATE TABLE noema.audit_event (
  tenant_id text NOT NULL,                                                     -- '__platform__' for platform events
  id text NOT NULL, actor_type text NOT NULL CHECK (actor_type IN ('principal','operator','system')), actor_id text NOT NULL,
  action text NOT NULL, resource_kind text NOT NULL, resource_id text, details jsonb NOT NULL DEFAULT '{}',   -- never content
  request_id text, at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, id));
REVOKE UPDATE, DELETE ON noema.audit_event FROM noema_app;                     -- append-only

-- jobs and outbox
CREATE TABLE jobs.job (                                                       -- tenant_id '__platform__' for periodic jobs
  id text PRIMARY KEY, tenant_id text NOT NULL, kind text NOT NULL, priority int NOT NULL DEFAULT 100,   -- lower = first
  payload jsonb NOT NULL, state text NOT NULL CHECK (state IN ('queued','running','done','dead')),
  attempts int NOT NULL DEFAULT 0, max_attempts int NOT NULL DEFAULT 5, run_after timestamptz NOT NULL DEFAULT now(),
  locked_by text, locked_until timestamptz, dedupe_key text, last_error text,  -- last_error: code + ≤ 200 chars, redacted
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'running') = (locked_by IS NOT NULL AND locked_until IS NOT NULL)));
CREATE INDEX job_ready ON jobs.job (priority, run_after) WHERE state = 'queued';          -- jobs.claim
CREATE INDEX job_running ON jobs.job (locked_until) WHERE state = 'running';               -- ops.reap_stuck
CREATE INDEX job_finished ON jobs.job (updated_at) WHERE state IN ('done','dead');          -- old-job cleanup (ops.reap_stuck)
CREATE UNIQUE INDEX job_dedupe ON jobs.job (tenant_id, kind, dedupe_key) WHERE dedupe_key IS NOT NULL AND state IN ('queued','running');
-- enqueue: INSERT ... ON CONFLICT (tenant_id, kind, dedupe_key) WHERE dedupe_key IS NOT NULL AND state IN ('queued','running')
--          DO NOTHING RETURNING id; no row -> SELECT the live duplicate's id
CREATE TABLE ops.outbox_dead_letter (
  kind text NOT NULL, id text NOT NULL, tenant_id text NOT NULL, payload jsonb NOT NULL, error text NOT NULL,
  attempts int NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, id));                    -- (kind, id): a labeling_offer and a decision_row share the decision id; definer-only

-- labeling and calibration (ADR-003, IMPL-072)
CREATE TABLE noema.labeling_run (
  tenant_id text NOT NULL, id text NOT NULL, profile_id text NOT NULL, profile_version text NOT NULL,
  question_name text NOT NULL, question_signature text NOT NULL,
  status text NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
  labelers text[] NOT NULL CHECK (cardinality(labelers) >= 2), rule text NOT NULL CHECK (rule = 'unanimous'),
  counts jsonb NOT NULL, consensus_rate numeric, consensus_rate_by_class jsonb, n_by_class jsonb, usage jsonb NOT NULL,
  started_at timestamptz NOT NULL, completed_at timestamptz, PRIMARY KEY (tenant_id, id));
CREATE TABLE noema.label_item (
  tenant_id text NOT NULL, id text NOT NULL, run_id text NOT NULL, decision_id text NOT NULL, question_name text NOT NULL,
  sample_reason text NOT NULL CHECK (sample_reason IN ('uniform','uncertainty')),
  labeler_answers jsonb NOT NULL,                                              -- [{model, status, value}] ≥ 2; per entry also alias,
                                                                               -- input_view, usage, cost_usd, cost_estimated, error
  kept bool NOT NULL, value jsonb,                                             -- consensus value iff kept
  drop_reason text CHECK (drop_reason IN ('disagreed','refused','parse_error','labeler_error')),
  ensemble jsonb NOT NULL,                                                     -- {value, probability, band, calibration_status} plus the
                                                                               -- fit inputs kind, options, members {id: {dist, unresolved, weight}}
  content_excerpt text, redacted bool NOT NULL,                                -- ≤ 2000 chars after redaction; NULL if policy forbids
  input_view_signature text NOT NULL, member_subset text[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, run_id) REFERENCES noema.labeling_run,
  UNIQUE (tenant_id, run_id, decision_id, question_name),                      -- a retried labeling.item job never duplicates
  CHECK (kept = (value IS NOT NULL)), CHECK (kept = (drop_reason IS NULL)));
CREATE UNIQUE INDEX labeling_run_open ON noema.labeling_run (tenant_id, profile_id, profile_version, question_name,
  question_signature) WHERE status = 'running';                                 -- LabelRepo.open_run: one open run per definition (rev 0002)
CREATE INDEX labeling_run_by_definition ON noema.labeling_run (tenant_id, profile_id, profile_version, question_signature);   -- rev 0002
CREATE INDEX label_item_kept ON noema.label_item (tenant_id, run_id, created_at) WHERE kept;   -- LabelRepo.kept_items (rev 0002)
CREATE INDEX label_item_dropped ON noema.label_item (tenant_id, id DESC) WHERE NOT kept;      -- listLabelingDropped
CREATE INDEX label_item_by_decision ON noema.label_item (tenant_id, decision_id);             -- retention (§5), items_for_decision
CREATE TABLE noema.calibrator (
  id text PRIMARY KEY, scope text NOT NULL CHECK (scope IN ('tenant','platform')), tenant_id text,
  profile_id text NOT NULL, profile_version text NOT NULL, question_name text NOT NULL, question_signature text NOT NULL,
  member_subset text[] NOT NULL, input_view_signature text NOT NULL,
  choice_method text NOT NULL DEFAULT 'flat' CHECK (choice_method IN ('flat','hierarchical')),
  method text NOT NULL CHECK (method IN ('temperature','platt')), member_t jsonb NOT NULL, pooled jsonb NOT NULL,
  epsilon numeric NOT NULL DEFAULT 0.0001, holdout jsonb NOT NULL, acceptance jsonb NOT NULL, reliability jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('fitted','rejected','accepted','active','downgraded','retired')),
  fitted_at timestamptz NOT NULL, accepted_at timestamptz, approved_by text, activated_by text[], activated_at timestamptz,
  downgraded_at timestamptz, downgrade_reason text CHECK (downgrade_reason IN ('drift_ece','drift_ci')),
  retired_at timestamptz, retired_reason text CHECK (retired_reason IN ('replaced','manual','profile_version_retired')),
  CHECK ((scope = 'platform') = (tenant_id IS NULL)),
  CHECK (status <> 'active' OR coalesce(cardinality(activated_by) = 2 AND activated_by[1] <> activated_by[2], false)));
  -- two-person; coalesce: a NULL activated_by (or element) makes the expression NULL, which a CHECK would pass
CREATE UNIQUE INDEX calibrator_one_active ON noema.calibrator
  (coalesce(tenant_id, '__platform__'), profile_id, profile_version, question_signature, member_subset, input_view_signature, choice_method)
  WHERE status = 'active';
CREATE TABLE noema.calibrator_epoch (
  scope_key text PRIMARY KEY,                                                  -- 'platform' or a tenant id
  n bigint NOT NULL DEFAULT 0, tenant_id text GENERATED ALWAYS AS (CASE WHEN scope_key = 'platform' THEN NULL ELSE scope_key END) STORED);
```

**Stored turn content** (format defined by the orchestrator, `noema.turns.context.history`; identity's `listSessionTurns` renders it the same way):
- `turn.input_parts`: the input items flattened in part order, each `{"type": "text", "text"}`, `{"type": "json", "json"}`, `{"type": "image", "file_id"}` or `{"type": "file", "file_id"}`; NULL under `metadata_only`. Labels are not stored (they are renumbered per turn).
- The user text is the text parts and the JSON parts (`orjson.dumps(json)`) joined by `"\n"`; the assistant text is `output_text`.
- `turn.redactions`: `{"user": [{start, end, reason}], "assistant": [...]}`, code-point spans of the stored `[REDACTED:<kind>]` markers over those texts (`reason` = `<kind>`). `jsonb` reorders object keys, so the user text rendered from a stored row can differ from the text the spans were computed on; readers take the spans from the markers in the text they render, and the stored user offsets are advisory when the turn has JSON parts. The wire (`SessionTurn.text`) shows each marker as `[redacted]`, with offsets recomputed.

## 3. Row-level security

`ENABLE` + `FORCE ROW LEVEL SECURITY` on every table in `noema`, `auth` and `jobs` (FORCE makes the policies bind `noema_owner` too). The `tenant_isolation` policy below goes on every one of them except `noema.tenant`, `noema.calibrator` and `noema.calibrator_epoch` (their policies follow). `platform.*` and `ops.outbox_dead_letter` hold no tenant data and have no RLS; their grants (§1) are the control.

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <t>
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
```

Project-scoped tables (`session`, `turn`, `decision`, `file`, `usage_line`, `idempotency_record`) add a second **restrictive** policy:

```sql
CREATE POLICY project_scope ON <t> AS RESTRICTIVE
  USING (coalesce(current_setting('app.project_id', true), '') = '' OR project_id = current_setting('app.project_id', true));
```

`uow(principal)` sets both settings with `set_config(name, value, true)` (transaction-local, bind parameters) as its first statement: `app.project_id = principal.project_id`, or `''` (tenant-wide) only for principals with `tenant_admin` or `auditor` (spec-09 §3). `tenant_uow(t, project_id=None)` sets `''`. A pooled connection outside a uow has both settings `''` or unset, which matches no row (`tenant_id <> ''`). The policies have no `FOR` clause, so `USING` also serves as `WITH CHECK` where none is given: an INSERT or UPDATE that would move a row to another tenant or project fails with an RLS violation.

Calibrators (IMPL-009 §4):

```sql
CREATE POLICY cal_read ON noema.calibrator FOR SELECT
  USING (tenant_id = current_setting('app.tenant_id', true) OR scope = 'platform');
CREATE POLICY cal_write ON noema.calibrator FOR ALL
  USING (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY epoch_read ON noema.calibrator_epoch FOR SELECT
  USING (scope_key = 'platform' OR scope_key = current_setting('app.tenant_id', true));
-- no write policy on calibrator_epoch for noema_app: increments only via admin.bump_epoch / labeling definer functions
CREATE POLICY tenant_read ON noema.tenant FOR SELECT USING (id = current_setting('app.tenant_id', true));
-- noema.tenant has no write policy: rows come only from admin.sync_tenants
```

`cal_read` OR `cal_write` decides SELECT, so a tenant sees its own rows and the platform rows; INSERT and UPDATE are decided by `cal_write` alone, so `noema_app` can never write a platform row or another tenant's row.

## 4. SECURITY DEFINER functions

Rules for every function below:
- owned by `noema_definer`; `SECURITY DEFINER`; `SET search_path = pg_catalog, pg_temp` (`pg_temp` last, so a temporary table can't shadow a relation) and every relation, function and type written schema-qualified (`auth.api_key`, `noema.principal`);
- `REVOKE ALL ON FUNCTION … FROM PUBLIC` (functions get `EXECUTE` for PUBLIC by default), then `GRANT EXECUTE` to the caller column only; the migration also runs `ALTER DEFAULT PRIVILEGES FOR ROLE noema_definer REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC`;
- parameters are used only as values (no dynamic SQL); each function returns the minimum columns, and the caller then opens `uow`/`tenant_uow` for the work;
- `LANGUAGE sql` or `plpgsql`, `STABLE` for the read-only ones.

```sql
CREATE FUNCTION auth.lookup_api_key(p_key_id text)
RETURNS TABLE (tenant_id text, project_id text, principal_id text, key_hash text, pepper_version int, env text,
               scopes text[], roles text[], expires_at timestamptz, revoked_at timestamptz, ip_allowlist cidr[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT k.tenant_id, k.project_id, k.principal_id, k.key_hash, k.pepper_version, k.env, k.scopes, p.roles,
         k.expires_at, k.revoked_at, k.ip_allowlist
    FROM auth.api_key k
    JOIN noema.principal p ON p.tenant_id = k.tenant_id AND p.id = k.principal_id AND p.disabled_at IS NULL
    JOIN noema.project j ON j.tenant_id = k.tenant_id AND j.id = k.project_id AND j.deleted_at IS NULL
    JOIN noema.tenant t ON t.id = k.tenant_id AND t.deleted_at IS NULL
   WHERE k.key_id = p_key_id $$;
REVOKE ALL ON FUNCTION auth.lookup_api_key(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.lookup_api_key(text) TO noema_app;
```

| Function | Returns / does | Caller |
|---|---|---|
| `auth.lookup_api_key(p_key_id text)` | as above, ≤ 1 row; disabled principals, deleted projects and deleted tenants return no row; `revoked_at`/`expires_at` are checked by the caller (spec-09 §2) | `noema_app` (`system_uow`) |
| `auth.touch_api_key(p_key_id text)` | `UPDATE … SET last_used_at = now() WHERE key_id = p_key_id AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')`; the authenticator calls it off the request path, at most once a minute per key (its LRU entry remembers the last touch) | `noema_app` |
| `auth.rehash_api_key(p_key_id text, p_hash text, p_pepper_version int) → bool` | pepper rotation; updates only if `p_pepper_version > pepper_version` (never a downgrade) | `noema_app` |
| `jobs.claim(p_kinds text[], p_n int, p_worker text, p_lock_s int)` | `UPDATE jobs.job SET state='running', attempts = attempts + 1, locked_by = p_worker, locked_until = now() + p_lock_s * interval '1 second', updated_at = now() WHERE id IN (SELECT id FROM jobs.job WHERE state='queued' AND run_after <= now() AND kind = ANY(p_kinds) ORDER BY priority, run_after FOR UPDATE SKIP LOCKED LIMIT p_n) RETURNING id, tenant_id, kind, payload, attempts` | `noema_app` |
| `jobs.extend(p_id text, p_worker text, p_lock_s int) → bool` | `locked_until = now() + p_lock_s` iff `state='running' AND locked_by = p_worker` (the fence: a reaped and re-claimed job is never extended or finished by its old worker) | `noema_app` |
| `jobs.finish(p_id text, p_worker text, p_ok bool, p_error text, p_permanent bool) → text` | iff `locked_by = p_worker`: ok → `done`; else `dead` if `p_permanent OR attempts >= max_attempts`, otherwise `queued` with `run_after = now() + 30 s × 2^(attempts − 1)`; clears the lock, sets `last_error`; returns the new state, NULL when the worker no longer held the job (rev 0002) | `noema_app` |
| `jobs.ensure_periodic(p_kind text, p_every interval)` | INSERT a `'__platform__'` job (`dedupe_key = p_kind`, `run_after = date_bin(p_every, now(), '2000-01-01') + p_every`) ON CONFLICT DO NOTHING (spec-11 §4) | `noema_app` |
| `ops.outbox_apply(p_records jsonb) → jsonb` | each record in its own subtransaction (plpgsql `BEGIN … EXCEPTION`), per kind as spec-11 §3; returns `[null | "<error code>"]` in input order | `noema_app` (writer, drainer) |
| `ops.outbox_dead(p_record jsonb, p_error text)` | INSERT `ops.outbox_dead_letter` ON CONFLICT (kind, id) DO NOTHING (two drainers may dead-letter the same entry) | `noema_app` |
| `ops.reap_stuck(p_older_than interval) → int` | jobs with `state='running' AND locked_until < now() - p_older_than` → `queued` (or `dead` at `max_attempts`); then deletes ≤ 1,000 jobs `done` for more than 7 days or `dead` for more than 30 days; returns the requeued count. Session leases need no reaper: `acquire` treats an expired lease as free (spec-04 §2) | `noema_app` (reaper) |
| `ops.retention_candidates(p_limit int)` | `(tenant_id, kind, id)` with `expires_at < now()` from `session`, `turn`, `decision`, `file`, `idempotency_record` (kind = the table name), oldest first, using the `*_expiry` indexes; superseded by `retention_page` | `noema_app` (sweeper; it deletes under `tenant_uow`) |
| `ops.retention_page(p_limit int, p_after_at timestamptz, p_after_kind text, p_after_id text, p_after_tenant text)` | `(tenant_id, kind, id, expires_at)` of the same rows in `(expires_at, kind, id, tenant_id)` order (text in the "C" collation), strictly after the given candidate (`p_after_at` NULL = from the start), so rows that keep failing never hold a page's head (rev 0002) | `noema_app` (sweeper) |
| `ops.calibrator_epochs()` | `(scope_key, n)` for every scope | `noema_app` (registry poll, 5 s) |
| `ops.active_calibrators(p_scope_keys text[])` | the `status='active'` rows of those scopes (`'platform'` = platform rows): every column except `acceptance` and `reliability` | `noema_app` (registry refresh) |
| `auth.lookup_operator(p_key_id text)` | `(id, name, key_hash, revoked_at)` | `noema_cli` |
| `admin.*`: `bootstrap_operator`, `create_operator`, `revoke_operator`, `config_propose`, `config_approve`, `config_rollback`, `sync_tenants`, `create_project`, `create_principal`, `create_api_key`, `revoke_api_key`, `calibrator_approve`, `calibrator_activate`, `calibrator_retire`, `upsert_platform_calibrator`, `set_calibrator_status`, `bump_epoch`, `import_prices`, `record_audit` (CLI actions outside the DB, e.g. ExternalId rotation), `rehash_operator` (operator keys after a pepper rotation: replaces the caller's `key_hash` only for a newer pepper version, never a downgrade; rev 0002), and the read-only `list_projects`, `list_api_keys` (no hashes), `list_calibrators`, `list_config_versions`, `config_body` (one stored config body, NULL for an unknown version; rev 0002) | each takes `p_operator_id`, enforces the two-person rules (approver ≠ proposer; activator ≠ approver), writes `audit_event` (`tenant_id = '__platform__'` or the tenant) and calls `pg_notify('noema_events', json)` for config, calibrator and key changes (bridged to Redis, spec-11 §4) | `noema_cli`; labeling jobs use `set_calibrator_status` and `bump_epoch` through a `noema_app` grant, and for a `noema_app` caller (`session_user`) `set_calibrator_status` allows only `active → downgraded` |

## 5. Retention and deletion

- Every tenant row with content carries `expires_at`, computed at insert from `platform.data_classes.<class>.retain_days` (spec-05) of the row's data class (session, else project `default_data_class`). `idempotency_record.expires_at = created_at + platform.idempotency.ttl_s`; the sweeper deletes the record's S3 object (`response_body_key`) with the row, so the body has the same TTL (IMPL-085).
- The hourly sweeper (ops-ledger) pages `ops.retention_page` (keyset on `(expires_at, kind, id, tenant_id)`, rev 0002) and deletes under `tenant_uow(tenant)`. Turns cascade to `attempt` and `turn_file`; files cascade to `derivative` rows, and their S3 objects are deleted (`FileService.purge`, the only path that deletes file objects). Deleting a decision sets `content_excerpt = NULL` on the `label_item` rows with that `decision_id` (labels are kept for calibration; their content is not).
- `DELETE /v1/sessions/{id}` → 202, `SessionRepo.mark_deleted`, plus a `session.delete` job that deletes, under `tenant_uow`, the session's turns (`TurnRepo.delete_for_session`), decisions, files (`FileService.purge`: S3 objects, then the rows), then the session row (`SessionRepo.delete`).
- L2 entries hold no content and expire within `decision_cache.ttl_s`. `purge_tenant` runs on tenant deletion.
- Old jobs are deleted by `ops.reap_stuck` (§4). `usage_line` and `audit_event` rows are not swept in Phase 1 (billing and audit records).

## 6. Tests the db role owns (`tests/integration/db/`)

- Migration up on an empty DB, and `alembic downgrade base` in CI only (production is forward-only, §7); the template DB `noema_tpl_<migration_hash>` under `pg_advisory_lock`; per-session clones (PLAN parallel-safety).
- **Catalog check**: every table in `noema`, `auth` and `jobs` has `relrowsecurity` and `relforcerowsecurity`; every table with a `tenant_id` column has it `NOT NULL` with the `<> ''` CHECK and a policy using it; `noema_app` has no privilege beyond §1 (`information_schema.role_table_grants`), no BYPASSRLS and no CREATE on any schema.
- **RLS matrix**: for every table, as `noema_app` with `app.tenant_id = 't_b'`, rows of `t_a` return 0 rows for SELECT and fail INSERT/UPDATE with an RLS violation. A missing or empty `app.tenant_id` returns 0 rows. The restrictive project policy is checked on the six project tables, including a tenant-wide (`''`) scope.
- Calibrators: a tenant sees its own rows and platform rows, never another tenant's. `noema_app` cannot insert or update platform rows. A two-person CHECK violation is rejected.
- Every SECURITY DEFINER function: `proconfig` contains `search_path=pg_catalog, pg_temp`; EXECUTE is denied to PUBLIC and to every role not in §4; a temporary table named like a referenced relation (`CREATE TEMP TABLE api_key …`) and a function in a user-writable schema have no effect; `auth.lookup_api_key` returns no row for a disabled principal, deleted project or deleted tenant.
- `jobs.extend`/`jobs.finish` with a stale `p_worker` change nothing; `jobs.finish` backoff and `dead` at `max_attempts`.
- `audit_event` UPDATE/DELETE are denied.
- Hot queries use their index on a seeded DB (`EXPLAIN`, no sequential scan): key lookup, session acquire, history by `seq`, `begin` of idempotency, `jobs.claim`, the usage report (both indexes), the retention scans.

## 7. Migration policy

- Alembic, run as `noema_owner` (`make db-migrate`), never by the app. One linear history; revision ids are `NNNN_<slug>`; the first revision is this file, and `0002_c2a_requests` (Wave C2a) adds `derivative.facts`, the per-signature `labeling_run_open` index, the `kept_items` indexes, `jobs.finish → text`, `ops.retention_page`, `admin.config_body` and `admin.rehash_operator` (marked "rev 0002" above).
- **Forward-only in production, expand → migrate → contract** (08:L475): additive changes ship first, code that uses them next, and a destructive change (drop, rename, type narrowing, NOT NULL on an existing column) waits a release. Data backfills run as `ops` jobs, not in migrations.
- Every new tenant table ships, in the same revision, with `tenant_id`, ENABLE + FORCE RLS, its policy and its §1 grants; the catalog check of §6 enforces it. Every new definer function follows the §4 rules and gets a §6 test.
- The migration hash (sha256 over the revision files) names the test template DB; `db/` is frozen after B2, so later revisions come only through change requests (spec-02 header).
