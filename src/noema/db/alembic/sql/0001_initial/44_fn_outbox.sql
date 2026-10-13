-- 0001_initial / 44: ops.outbox_apply and ops.outbox_dead (spec-03 §4, spec-11 §3; IMPL-022).
SET LOCAL ROLE noema_definer;

-- turn_finalize: {lease: {session_id, turn_id, last_seq} | null, turn, attempts[], decisions[], turn_files[],
-- sticky_spec}. An existing turn row means a replay (the first apply committed the rows and the session update
-- together). With a lease the session advances iff last_seq still equals lease.last_seq, else seq_conflict.
CREATE FUNCTION ops._apply_turn_finalize(p_tenant_id text, p_id text, p jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_turn jsonb := ops._row(p->'turn', p_tenant_id, p_id);
  v_lease jsonb := nullif(p->'lease', 'null'::jsonb);
  v_last_seq int;
  v_rows int;
  v_x jsonb;
BEGIN
  IF EXISTS (SELECT 1 FROM noema.turn t WHERE t.tenant_id = p_tenant_id AND t.id = p_id) THEN
    RETURN;
  END IF;
  IF v_lease IS NOT NULL THEN
    v_last_seq := (v_lease->>'last_seq')::int;
    IF v_last_seq IS NULL OR v_lease->>'session_id' IS NULL
       OR v_lease->>'session_id' IS DISTINCT FROM v_turn->>'session_id'
       OR (v_turn->>'seq')::int IS DISTINCT FROM v_last_seq + 1 THEN
      RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'invalid_record';
    END IF;
    UPDATE noema.session s
       SET last_seq = v_last_seq + 1, sticky_spec = nullif(p->'sticky_spec', 'null'::jsonb),
           active_turn_id = NULL, lease_expires_at = NULL
     WHERE s.tenant_id = p_tenant_id AND s.id = v_lease->>'session_id' AND s.last_seq = v_last_seq;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
      RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'seq_conflict';
    END IF;
  END IF;
  PERFORM ops._insert_turn(v_turn);
  FOR v_x IN SELECT e.value FROM jsonb_array_elements(coalesce(p->'attempts', '[]'::jsonb)) e LOOP
    PERFORM ops._insert_attempt(ops._row(v_x, p_tenant_id, NULL));
  END LOOP;
  FOR v_x IN SELECT e.value FROM jsonb_array_elements(coalesce(p->'decisions', '[]'::jsonb)) e LOOP
    PERFORM ops._insert_decision(ops._row(v_x, p_tenant_id, NULL));
  END LOOP;
  FOR v_x IN SELECT e.value FROM jsonb_array_elements(coalesce(p->'turn_files', '[]'::jsonb)) e LOOP
    PERFORM ops._insert_turn_file(ops._row(v_x, p_tenant_id, NULL));
  END LOOP;
END $$;

-- labeling_offer: {decision_id, turn_id, question_names[], sample_reason} -> one labeling.item job (priority 200,
-- dedupe_key = decision_id). The job id derives from the decision id, so a replay after the job finished is a no-op.
CREATE FUNCTION ops._apply_labeling_offer(p_tenant_id text, p_id text, p jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p ? 'decision_id' AND p->>'decision_id' IS DISTINCT FROM p_id THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'id_mismatch';
  END IF;
  INSERT INTO jobs.job (id, tenant_id, kind, priority, payload, state, dedupe_key)
  VALUES ('job_' || CASE WHEN p_id LIKE 'dec\_%' THEN substr(p_id, 5) ELSE p_id END, p_tenant_id, 'labeling.item',
          200, p, 'queued', p_id)
  ON CONFLICT DO NOTHING;
  PERFORM pg_notify('jobs', 'labeling.item');
END $$;

CREATE FUNCTION ops._apply_one(p_record jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_kind text := p_record->>'kind';
  v_id text := p_record->>'id';
  v_tenant text := p_record->>'tenant_id';
  v_payload jsonb := p_record->'payload';
BEGIN
  IF coalesce(v_id, '') = '' OR coalesce(v_tenant, '') = '' OR jsonb_typeof(v_payload) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'invalid_record';
  END IF;
  CASE v_kind
    WHEN 'usage_line' THEN PERFORM ops._insert_usage_line(ops._row(v_payload, v_tenant, v_id));
    WHEN 'audit_event' THEN PERFORM ops._insert_audit_event(ops._row(v_payload, v_tenant, v_id));
    WHEN 'decision_row' THEN PERFORM ops._insert_decision(ops._row(v_payload, v_tenant, v_id));
    WHEN 'turn_finalize' THEN PERFORM ops._apply_turn_finalize(v_tenant, v_id, v_payload);
    WHEN 'labeling_offer' THEN PERFORM ops._apply_labeling_offer(v_tenant, v_id, v_payload);
    ELSE RAISE EXCEPTION USING ERRCODE = 'NOE01', MESSAGE = 'unknown_kind';
  END CASE;
END $$;

-- Each record in its own subtransaction; returns [null | "<error code>"] in input order. Error codes: the NOE01
-- reasons above (invalid_record, tenant_mismatch, id_mismatch, seq_conflict, unknown_kind) or
-- 'sqlstate_<code>[:<constraint>]'; they never contain payload values.
CREATE FUNCTION ops.outbox_apply(p_records jsonb) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_rec jsonb;
  v_out jsonb := '[]'::jsonb;
  v_state text;
  v_msg text;
  v_constraint text;
BEGIN
  IF p_records IS NULL OR jsonb_typeof(p_records) <> 'array' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: p_records must be a JSON array';
  END IF;
  FOR v_rec IN SELECT e.value FROM jsonb_array_elements(p_records) e LOOP
    BEGIN
      PERFORM ops._apply_one(v_rec);
      v_out := v_out || jsonb_build_array(NULL::text);
    EXCEPTION WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE, v_msg = MESSAGE_TEXT, v_constraint = CONSTRAINT_NAME;
      v_out := v_out || jsonb_build_array(CASE WHEN v_state = 'NOE01' THEN v_msg
                                               ELSE 'sqlstate_' || v_state || coalesce(':' || nullif(v_constraint, ''), '')
                                          END);
    END;
  END LOOP;
  RETURN v_out;
END $$;

-- Dead-letters a record (two drainers may do it for the same entry). p_record = {kind, id, tenant_id, payload,
-- session_id?, attempts?}.
CREATE FUNCTION ops.outbox_dead(p_record jsonb, p_error text) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  INSERT INTO ops.outbox_dead_letter (kind, id, tenant_id, payload, error, attempts)
  VALUES (p_record->>'kind', p_record->>'id', p_record->>'tenant_id', coalesce(p_record->'payload', '{}'::jsonb),
          left(coalesce(p_error, ''), 1000), coalesce((p_record->>'attempts')::int, 0))
  ON CONFLICT (kind, id) DO NOTHING $$;

RESET ROLE;
