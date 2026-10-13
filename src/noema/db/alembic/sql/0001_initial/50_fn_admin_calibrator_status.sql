-- 0001_initial / 50: calibrator status transitions, platform artifacts and the listing (spec-03 §4; IMPL-072).
SET LOCAL ROLE noema_definer;

-- The one transition routine. A 'system' actor (noema_app, labeling jobs) may only downgrade an active artifact.
CREATE FUNCTION admin._transition(p_actor_id text, p_actor_type text, p_calibrator_id text, p_status text,
                                  p_reason text) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_c noema.calibrator%ROWTYPE;
BEGIN
  SELECT * INTO v_c FROM noema.calibrator c WHERE c.id = p_calibrator_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: calibrator';
  END IF;
  IF p_actor_type = 'system' AND NOT (v_c.status = 'active' AND p_status = 'downgraded') THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE16', MESSAGE = 'forbidden: noema_app may only downgrade an active calibrator';
  END IF;
  IF p_status IN ('accepted', 'rejected') AND v_c.status = 'fitted' THEN
    UPDATE noema.calibrator c
       SET status = p_status, accepted_at = CASE WHEN p_status = 'accepted' THEN now() ELSE c.accepted_at END
     WHERE c.id = p_calibrator_id;
  ELSIF p_status = 'downgraded' AND v_c.status = 'active' THEN
    IF p_reason IS NULL OR p_reason NOT IN ('drift_ece', 'drift_ci') THEN
      RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: a downgrade reason is drift_ece or drift_ci';
    END IF;
    UPDATE noema.calibrator c SET status = 'downgraded', downgraded_at = now(), downgrade_reason = p_reason
     WHERE c.id = p_calibrator_id;
  ELSIF p_status = 'retired' AND v_c.status <> 'retired' THEN
    IF p_reason IS NULL OR p_reason NOT IN ('manual', 'profile_version_retired', 'replaced') THEN
      RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: a retire reason is manual, profile_version_retired or replaced';
    END IF;
    UPDATE noema.calibrator c SET status = 'retired', retired_at = now(), retired_reason = p_reason
     WHERE c.id = p_calibrator_id;
  ELSE
    RAISE EXCEPTION USING ERRCODE = 'NOE13',
      MESSAGE = pg_catalog.format('invalid_state: calibrator transition %s -> %s is not allowed here', v_c.status, p_status);
  END IF;
  IF v_c.status = 'active' THEN
    PERFORM admin._bump(admin._scope_key(v_c.scope, v_c.tenant_id));
  END IF;
  PERFORM admin._audit(v_c.tenant_id, p_actor_type, p_actor_id, 'calibrator.status', 'calibrator', p_calibrator_id,
                       jsonb_build_object('from', v_c.status, 'to', p_status, 'reason', p_reason));
END $$;

-- noema_cli: fitted -> accepted|rejected, active -> downgraded, * -> retired. noema_app (labeling drift job, actor
-- 'system:<purpose>'): active -> downgraded only. Activation is calibrator_activate only.
CREATE FUNCTION admin.set_calibrator_status(p_operator_id text, p_calibrator_id text, p_status text,
                                            p_reason text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._transition(p_operator_id, admin._actor(p_operator_id), p_calibrator_id, p_status, p_reason);
END $$;

-- A platform artifact (fitted on seed data) as fitted, accepted or rejected; an update resets its approval.
-- p_row uses noema.calibrator column names; id is required ('cal_<ulid>').
CREATE FUNCTION admin.upsert_platform_calibrator(p_operator_id text, p_row jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  r noema.calibrator%ROWTYPE;
  v_existing text;
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF jsonb_typeof(p_row) IS DISTINCT FROM 'object' OR coalesce(p_row->>'scope', 'platform') <> 'platform'
     OR (p_row ? 'tenant_id' AND jsonb_typeof(p_row->'tenant_id') <> 'null') THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: a platform calibrator has no tenant_id';
  END IF;
  r := jsonb_populate_record(NULL::noema.calibrator, p_row);
  IF r.id IS NULL OR r.id !~ '^cal_' OR r.status IS NULL OR r.status NOT IN ('fitted', 'accepted', 'rejected') THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: id cal_* and status fitted|accepted|rejected';
  END IF;
  SELECT c.status INTO v_existing FROM noema.calibrator c WHERE c.id = r.id FOR UPDATE;
  IF FOUND THEN
    IF v_existing NOT IN ('fitted', 'accepted', 'rejected')
       OR NOT EXISTS (SELECT 1 FROM noema.calibrator c WHERE c.id = r.id AND c.scope = 'platform') THEN
      RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: only an inactive platform artifact can be replaced';
    END IF;
    UPDATE noema.calibrator c
       SET profile_id = r.profile_id, profile_version = r.profile_version, question_name = r.question_name,
           question_signature = r.question_signature, member_subset = r.member_subset,
           input_view_signature = r.input_view_signature, choice_method = coalesce(r.choice_method, 'flat'),
           method = r.method, member_t = r.member_t, pooled = r.pooled, epsilon = coalesce(r.epsilon, 0.0001),
           holdout = r.holdout, acceptance = r.acceptance, reliability = r.reliability, status = r.status,
           fitted_at = r.fitted_at, accepted_at = r.accepted_at, approved_by = NULL, activated_by = NULL,
           activated_at = NULL
     WHERE c.id = r.id;
  ELSE
    INSERT INTO noema.calibrator (id, scope, tenant_id, profile_id, profile_version, question_name, question_signature,
                                  member_subset, input_view_signature, choice_method, method, member_t, pooled,
                                  epsilon, holdout, acceptance, reliability, status, fitted_at, accepted_at)
    VALUES (r.id, 'platform', NULL, r.profile_id, r.profile_version, r.question_name, r.question_signature,
            r.member_subset, r.input_view_signature, coalesce(r.choice_method, 'flat'), r.method, r.member_t, r.pooled,
            coalesce(r.epsilon, 0.0001), r.holdout, r.acceptance, r.reliability, r.status, r.fitted_at, r.accepted_at);
  END IF;
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'calibrator.upsert_platform', 'calibrator', r.id,
                       jsonb_build_object('status', r.status));
  RETURN r.id;
END $$;

-- p_scope_key: 'platform', a tenant id, or NULL for every scope.
CREATE FUNCTION admin.list_calibrators(p_operator_id text, p_scope_key text DEFAULT NULL, p_status text DEFAULT NULL)
RETURNS TABLE (id text, scope text, tenant_id text, profile_id text, profile_version text, question_name text,
               question_signature text, member_subset text[], input_view_signature text, choice_method text,
               method text, status text, holdout jsonb, acceptance jsonb, fitted_at timestamptz,
               accepted_at timestamptz, approved_by text, activated_by text[], activated_at timestamptz,
               downgraded_at timestamptz, downgrade_reason text, retired_at timestamptz, retired_reason text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  RETURN QUERY
    SELECT c.id, c.scope, c.tenant_id, c.profile_id, c.profile_version, c.question_name, c.question_signature,
           c.member_subset, c.input_view_signature, c.choice_method, c.method, c.status, c.holdout, c.acceptance,
           c.fitted_at, c.accepted_at, c.approved_by, c.activated_by, c.activated_at, c.downgraded_at,
           c.downgrade_reason, c.retired_at, c.retired_reason
      FROM noema.calibrator c
     WHERE (p_scope_key IS NULL OR admin._scope_key(c.scope, c.tenant_id) = p_scope_key)
       AND (p_status IS NULL OR c.status = p_status)
     ORDER BY c.id DESC;
END $$;

RESET ROLE;
