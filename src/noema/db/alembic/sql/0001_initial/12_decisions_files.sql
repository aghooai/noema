-- 0001_initial / 12: decisions (IMPL-071; verification results are decision rows with purpose = 'verification'),
-- files and derivatives (spec-03 §2, spec-07).
CREATE TABLE noema.decision (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL,
  created_at timestamptz NOT NULL,
  principal_id text NOT NULL,                                                  -- the caller (getDecision "own", spec-09 §3)
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
CREATE INDEX decision_by_session ON noema.decision (tenant_id, session_id) WHERE session_id IS NOT NULL;
CREATE INDEX decision_expiry ON noema.decision (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE noema.file (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL,
  principal_id text NOT NULL, session_id text,
  origin text NOT NULL CHECK (origin IN ('upload','inline')),
  filename_display text,                                                       -- display only; never sent to models
  declared_type text NOT NULL, media_type text,                                -- media_type NULL until detect
  bytes bigint NOT NULL, sha256 text NOT NULL,
  status text NOT NULL CHECK (status IN ('quarantined','processing','ready','partial','rejected','failed','deleted')),
  rejection jsonb, failure jsonb, quarantine_key text NOT NULL, pages int,
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz, expires_at timestamptz,
  PRIMARY KEY (tenant_id, id));
CREATE INDEX file_by_session ON noema.file (tenant_id, session_id) WHERE session_id IS NOT NULL;
CREATE INDEX file_expiry ON noema.file (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE noema.derivative (                                                -- raster_1568 is internal (spec-02 §6)
  tenant_id text NOT NULL CHECK (tenant_id <> ''), id text NOT NULL, file_id text NOT NULL,
  kind text NOT NULL
    CHECK (kind IN ('raster','raster_1568','thumbnail','ocr_text','pdf_sanitized','markdown','page_images','text')),
  status text NOT NULL CHECK (status IN ('pending','ready','failed')), extractor_version text NOT NULL,
  sha256 text, bytes bigint, pages int, width int, height int,                 -- width/height: single-image kinds
  text_layer_pages int[]                     -- page_images: 1-based pages with a text layer (pdf.render facts, ADR-002 §5)
    CHECK (text_layer_pages IS NULL OR (kind = 'page_images' AND 0 < ALL (text_layer_pages))),
  storage_key text, error_code text,
  created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, file_id, kind),
  FOREIGN KEY (tenant_id, file_id) REFERENCES noema.file ON DELETE CASCADE);
-- S3 layout: s3://noema-<env>/<tenant_id>/quarantine/<file_id>; .../derivatives/<file_id>/<kind>[/page-NNNN.png];
--            .../idempotency/<idempotency_record.id> (response bodies > 1 MiB, IMPL-085)
