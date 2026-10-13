-- 0001_initial / 43: row helpers of ops.outbox_apply (spec-11 §3). Each insert is ON CONFLICT (primary key) DO
-- NOTHING, so a replayed record is a no-op. Payload rows use spec-03 column names; missing optional keys are NULL.
SET LOCAL ROLE noema_definer;

-- The payload row with tenant_id forced to the record's tenant; a row of another tenant (or another id, when
-- p_id is given) is refused, so a record can never write outside its own tenant.
CREATE FUNCTION ops._row(p_row jsonb, p_tenant_id text, p_id text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p_row IS NULL OR jsonb_typeof(p_row) <> 'object' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'invalid_record';
  END IF;
  IF p_row ? 'tenant_id' AND p_row->>'tenant_id' IS DISTINCT FROM p_tenant_id THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'tenant_mismatch';
  END IF;
  IF p_id IS NOT NULL AND p_row ? 'id' AND p_row->>'id' IS DISTINCT FROM p_id THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'id_mismatch';
  END IF;
  IF p_id IS NOT NULL THEN
    RETURN p_row || jsonb_build_object('tenant_id', p_tenant_id, 'id', p_id);
  END IF;
  RETURN p_row || jsonb_build_object('tenant_id', p_tenant_id);
END $$;

CREATE FUNCTION ops._insert_decision(p_row jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO noema.decision (tenant_id, project_id, id, created_at, principal_id, turn_id, session_id, purpose, origin,
                              output_format, profile_id, profile_version, question_signature, input_view_signature,
                              input_digest, status, cache_hit, safety_identifier, questions, input, result, expires_at)
  SELECT r.tenant_id, r.project_id, r.id, r.created_at, r.principal_id, r.turn_id, r.session_id, r.purpose, r.origin,
         r.output_format, r.profile_id, r.profile_version, r.question_signature, r.input_view_signature,
         r.input_digest, r.status, r.cache_hit, r.safety_identifier, r.questions, r.input, r.result, r.expires_at
    FROM jsonb_populate_record(NULL::noema.decision, p_row) r
  ON CONFLICT (tenant_id, id) DO NOTHING $$;

CREATE FUNCTION ops._insert_turn(p_row jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO noema.turn (tenant_id, project_id, id, session_id, seq, principal_id, status, content_policy, input_parts,
                          output_text, redactions, generation_profile, routing, usage, timings, degradations,
                          decision_id, verification_decision_id, retry_of, created_at, completed_at, expires_at)
  SELECT r.tenant_id, r.project_id, r.id, r.session_id, r.seq, r.principal_id, r.status, r.content_policy,
         r.input_parts, r.output_text, coalesce(r.redactions, '{"user": [], "assistant": []}'::jsonb),
         r.generation_profile, r.routing, r.usage, r.timings, coalesce(r.degradations, '[]'::jsonb), r.decision_id,
         r.verification_decision_id, r.retry_of, r.created_at, r.completed_at, r.expires_at
    FROM jsonb_populate_record(NULL::noema.turn, p_row) r
  ON CONFLICT (tenant_id, id) DO NOTHING $$;

CREATE FUNCTION ops._insert_attempt(p_row jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO noema.attempt (tenant_id, id, turn_id, route_id, model, spec_signature, outcome, cancel_reason,
                             finish_reason, error_code, usage, cost_usd, cost_estimated, started_at, ended_at)
  SELECT r.tenant_id, r.id, r.turn_id, r.route_id, r.model, r.spec_signature, r.outcome, r.cancel_reason,
         r.finish_reason, r.error_code, r.usage, r.cost_usd, r.cost_estimated, r.started_at, r.ended_at
    FROM jsonb_populate_record(NULL::noema.attempt, p_row) r
  ON CONFLICT (tenant_id, id) DO NOTHING $$;

CREATE FUNCTION ops._insert_turn_file(p_row jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO noema.turn_file (tenant_id, turn_id, part_index, file_id)
  SELECT r.tenant_id, r.turn_id, r.part_index, r.file_id FROM jsonb_populate_record(NULL::noema.turn_file, p_row) r
  ON CONFLICT (tenant_id, turn_id, part_index) DO NOTHING $$;

CREATE FUNCTION ops._insert_usage_line(p_row jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO noema.usage_line (tenant_id, project_id, id, line, model_id, region, session_id, turn_id, decision_id,
                                attempt_id, labeling_run_id, input_tokens, output_tokens, cached_input_tokens,
                                cache_write_input_tokens, reasoning_tokens, tokens_estimated, cost_usd,
                                cost_estimated, at)
  SELECT r.tenant_id, r.project_id, r.id, r.line, r.model_id, r.region, r.session_id, r.turn_id, r.decision_id,
         r.attempt_id, r.labeling_run_id, r.input_tokens, r.output_tokens, r.cached_input_tokens,
         r.cache_write_input_tokens, r.reasoning_tokens, r.tokens_estimated, r.cost_usd, r.cost_estimated, r.at
    FROM jsonb_populate_record(NULL::noema.usage_line, p_row) r
  ON CONFLICT (tenant_id, id) DO NOTHING $$;

CREATE FUNCTION ops._insert_audit_event(p_row jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO noema.audit_event (tenant_id, id, actor_type, actor_id, action, resource_kind, resource_id, details,
                                 request_id, at)
  SELECT r.tenant_id, r.id, r.actor_type, r.actor_id, r.action, r.resource_kind, r.resource_id,
         coalesce(r.details, '{}'::jsonb), r.request_id, coalesce(r.at, now())
    FROM jsonb_populate_record(NULL::noema.audit_event, p_row) r
  ON CONFLICT (tenant_id, id) DO NOTHING $$;

RESET ROLE;
