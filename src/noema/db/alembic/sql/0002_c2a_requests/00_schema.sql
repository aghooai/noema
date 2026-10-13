-- 0002_c2a_requests / 00: tables and indexes of the Wave C2a change requests (spec-03 §2). Runs as noema_owner.

-- labeling 9: a run holds one question definition, so the one running run is per (name, signature): facade
-- questions are all named q0, q1, ... and tenants reuse names across definitions.
DROP INDEX noema.labeling_run_open;
CREATE UNIQUE INDEX labeling_run_open ON noema.labeling_run
  (tenant_id, profile_id, profile_version, question_name, question_signature) WHERE status = 'running';

-- labeling 4b: LabelRepo.kept_items (one definition's kept labels since a time, created_at order: the fit and drift
-- windows). 4a (items_for_decision) uses label_item_by_decision of 0001.
CREATE INDEX labeling_run_by_definition ON noema.labeling_run
  (tenant_id, profile_id, profile_version, question_signature);
CREATE INDEX label_item_kept ON noema.label_item (tenant_id, run_id, created_at) WHERE kept;

-- media-pipeline 2: the pdf.sanitize facts that outlive the sandbox run (spec-09 §6.3 step 5), closed per kind:
-- pdf_sanitized {"active_content": [<the distinct spec-09 §6.3 fact values, sorted>]}; every other kind NULL.
-- (pages is its own column; page_images' text layer is text_layer_pages.) CASE fixes the evaluation order, so a
-- non-object never reaches the jsonb '-' operator.
ALTER TABLE noema.derivative ADD COLUMN facts jsonb
  CONSTRAINT derivative_facts_closed CHECK (facts IS NULL OR (kind = 'pdf_sanitized' AND CASE
    WHEN jsonb_typeof(facts) <> 'object' THEN false
    WHEN (facts - 'active_content') <> '{}'::jsonb THEN false
    WHEN jsonb_typeof(facts -> 'active_content') IS DISTINCT FROM 'array' THEN false
    ELSE (facts -> 'active_content') <@ '["additional_actions", "embedded_file", "javascript", "launch",
                                          "open_action", "remote_action", "rich_media", "xfa"]'::jsonb
  END));
