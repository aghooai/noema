-- 0001_initial / 14: labeling and calibration (spec-03 §2; ADR-003, IMPL-072).
CREATE TABLE noema.labeling_run (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), id text NOT NULL, profile_id text NOT NULL,
  profile_version text NOT NULL, question_name text NOT NULL, question_signature text NOT NULL,
  status text NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
  labelers text[] NOT NULL CHECK (cardinality(labelers) >= 2), rule text NOT NULL CHECK (rule = 'unanimous'),
  counts jsonb NOT NULL, consensus_rate numeric, consensus_rate_by_class jsonb, n_by_class jsonb, usage jsonb NOT NULL,
  started_at timestamptz NOT NULL, completed_at timestamptz, PRIMARY KEY (tenant_id, id));

CREATE TABLE noema.label_item (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), id text NOT NULL, run_id text NOT NULL, decision_id text NOT NULL,
  question_name text NOT NULL,
  sample_reason text NOT NULL CHECK (sample_reason IN ('uniform','uncertainty')),
  labeler_answers jsonb NOT NULL,                                              -- [{model, status, value}] >= 2
  kept bool NOT NULL, value jsonb,                                             -- consensus value iff kept
  drop_reason text CHECK (drop_reason IN ('disagreed','refused','parse_error','labeler_error')),
  ensemble jsonb NOT NULL,                                                     -- {value, probability, band, calibration_status}
  content_excerpt text, redacted bool NOT NULL,                                -- <= 2000 chars after redaction
  input_view_signature text NOT NULL, member_subset text[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, run_id) REFERENCES noema.labeling_run,
  UNIQUE (tenant_id, run_id, decision_id, question_name),                      -- a retried labeling.item never duplicates
  CHECK (kept = (value IS NOT NULL)), CHECK (kept = (drop_reason IS NULL)));
CREATE UNIQUE INDEX labeling_run_open ON noema.labeling_run (tenant_id, profile_id, profile_version, question_name)
  WHERE status = 'running';                                                     -- LabelRepo.open_run
CREATE INDEX label_item_dropped ON noema.label_item (tenant_id, id DESC) WHERE NOT kept;      -- listLabelingDropped
CREATE INDEX label_item_by_decision ON noema.label_item (tenant_id, decision_id);             -- retention (§5)

CREATE TABLE noema.calibrator (
  id text PRIMARY KEY, scope text NOT NULL CHECK (scope IN ('tenant','platform')), tenant_id text CHECK (tenant_id <> ''),
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
  -- two-person; coalesce: a NULL activated_by (or element) must fail the CHECK, not pass it as unknown
  CHECK (status <> 'active' OR coalesce(cardinality(activated_by) = 2 AND activated_by[1] <> activated_by[2], false)));
CREATE UNIQUE INDEX calibrator_one_active ON noema.calibrator
  (coalesce(tenant_id, '__platform__'), profile_id, profile_version, question_signature, member_subset,
   input_view_signature, choice_method)
  WHERE status = 'active';

CREATE TABLE noema.calibrator_epoch (
  scope_key text PRIMARY KEY CHECK (scope_key <> ''),                          -- 'platform' or a tenant id
  n bigint NOT NULL DEFAULT 0,
  tenant_id text GENERATED ALWAYS AS (CASE WHEN scope_key = 'platform' THEN NULL ELSE scope_key END) STORED);
