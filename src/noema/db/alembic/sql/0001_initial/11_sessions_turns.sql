-- 0001_initial / 11: sessions, turns, turn files and attempts (spec-03 §2; IMPL-011, IMPL-014, IMPL-018, IMPL-022).
CREATE TABLE noema.session (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL,
  owner_principal_id text NOT NULL,
  data_class text NOT NULL CHECK (data_class IN ('public','internal','confidential','restricted')),
  retention text NOT NULL DEFAULT 'default' CHECK (retention IN ('default','ephemeral')),   -- custom: 422 in Phase 1
  expires_at timestamptz, metadata jsonb NOT NULL DEFAULT '{}',
  last_seq int NOT NULL DEFAULT 0, sticky_spec jsonb,                         -- IMPL-014; AttemptSpec fields as JSON
  active_turn_id text, lease_expires_at timestamptz,                           -- session ordering lease (IMPL-022)
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, project_id) REFERENCES noema.project,
  CHECK ((active_turn_id IS NULL) = (lease_expires_at IS NULL)));
CREATE INDEX session_by_project ON noema.session (tenant_id, project_id, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX session_by_owner ON noema.session (tenant_id, owner_principal_id, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX session_expiry ON noema.session (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE noema.turn (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL, session_id text, seq int,
  principal_id text NOT NULL, status text NOT NULL CHECK (status IN ('completed','failed','cancelled','abandoned')),
  content_policy text NOT NULL CHECK (content_policy IN ('full','redacted','metadata_only')),
  input_parts jsonb, output_text text,                                         -- per content_policy (IMPL-018)
  redactions jsonb NOT NULL DEFAULT '{"user": [], "assistant": []}',           -- SessionTurn.redactions (code points)
  generation_profile text, routing jsonb NOT NULL, usage jsonb NOT NULL, timings jsonb NOT NULL,
  degradations jsonb NOT NULL DEFAULT '[]', decision_id text, verification_decision_id text,
  retry_of text,                                                               -- unused in Phase 1
  created_at timestamptz NOT NULL, completed_at timestamptz NOT NULL, expires_at timestamptz,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, session_id, seq),            -- the unique index serves history by seq
  FOREIGN KEY (tenant_id, session_id) REFERENCES noema.session, CHECK ((session_id IS NULL) = (seq IS NULL)));
CREATE INDEX turn_expiry ON noema.turn (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE noema.turn_file (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), turn_id text NOT NULL, part_index int NOT NULL, file_id text NOT NULL,
  PRIMARY KEY (tenant_id, turn_id, part_index),
  FOREIGN KEY (tenant_id, turn_id) REFERENCES noema.turn ON DELETE CASCADE);

CREATE TABLE noema.attempt (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), id text NOT NULL, turn_id text NOT NULL, route_id text NOT NULL,
  model text NOT NULL,                                                          -- Bedrock id (TurnObject.attempts[].model)
  spec_signature text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('committed','cancelled','failed')),   -- IMPL-011
  cancel_reason text CHECK (cancel_reason IN ('spec_changed','lost_race','client_abort','failed','deadline')),
  finish_reason text CHECK (finish_reason IN ('stop','length','content_filter','cancelled')), error_code text,
  usage jsonb NOT NULL, cost_usd numeric(18,6) NOT NULL, cost_estimated bool NOT NULL,
  started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, turn_id) REFERENCES noema.turn ON DELETE CASCADE);
CREATE INDEX attempt_by_turn ON noema.attempt (tenant_id, turn_id, started_at);
