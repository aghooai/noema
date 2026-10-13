-- 0001_initial / 49: calibrator lifecycle and epochs (spec-03 §4; ADR-003 §1-§2, §5; IMPL-072).
-- fitted -> accepted | rejected; accepted -> active by two people (approve by A, activate by B != A);
-- active -> downgraded (drift) | retired. Any change of an active artifact bumps its scope's epoch.
SET LOCAL ROLE noema_definer;

-- Increments the epoch of one scope ('platform' or a tenant id) and notifies calibrators_changed.
CREATE FUNCTION admin._bump(p_scope_key text) RETURNS bigint
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_n bigint;
BEGIN
  INSERT INTO noema.calibrator_epoch AS e (scope_key, n) VALUES (p_scope_key, 1)
  ON CONFLICT (scope_key) DO UPDATE SET n = e.n + 1
  RETURNING e.n INTO v_n;
  PERFORM admin._notify('calibrators_changed', jsonb_build_object('scope_key', p_scope_key, 'n', v_n));
  RETURN v_n;
END $$;

CREATE FUNCTION admin._scope_key(p_scope text, p_tenant_id text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
  SELECT CASE WHEN p_scope = 'platform' THEN 'platform' ELSE p_tenant_id END $$;

CREATE FUNCTION admin.bump_epoch(p_operator_id text, p_scope_key text) RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_actor text := admin._actor(p_operator_id);
  v_n bigint;
BEGIN
  IF p_scope_key IS DISTINCT FROM 'platform'
     AND NOT EXISTS (SELECT 1 FROM noema.tenant t WHERE t.id = p_scope_key AND t.deleted_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: scope';
  END IF;
  v_n := admin._bump(p_scope_key);
  PERFORM admin._audit(nullif(p_scope_key, 'platform'), v_actor, p_operator_id, 'calibrator.epoch_bump',
                       'calibrator_epoch', p_scope_key, jsonb_build_object('n', v_n));
  RETURN v_n;
END $$;

CREATE FUNCTION admin.calibrator_approve(p_operator_id text, p_calibrator_id text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_c noema.calibrator%ROWTYPE;
BEGIN
  PERFORM admin._operator(p_operator_id);
  SELECT * INTO v_c FROM noema.calibrator c WHERE c.id = p_calibrator_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: calibrator';
  END IF;
  IF v_c.status <> 'accepted' OR v_c.approved_by IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: only an accepted, unapproved calibrator can be approved';
  END IF;
  UPDATE noema.calibrator c SET approved_by = p_operator_id WHERE c.id = p_calibrator_id;
  PERFORM admin._audit(v_c.tenant_id, 'operator', p_operator_id, 'calibrator.approve', 'calibrator', p_calibrator_id,
                       '{}'::jsonb);
END $$;

-- The second person: activates an approved artifact and retires the active one of the same scope and binding.
CREATE FUNCTION admin.calibrator_activate(p_operator_id text, p_calibrator_id text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_c noema.calibrator%ROWTYPE;
  v_replaced text[];
BEGIN
  PERFORM admin._operator(p_operator_id);
  SELECT * INTO v_c FROM noema.calibrator c WHERE c.id = p_calibrator_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: calibrator';
  END IF;
  IF v_c.status <> 'accepted' OR v_c.approved_by IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: only an accepted and approved calibrator can be activated';
  END IF;
  IF v_c.approved_by = p_operator_id THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE11', MESSAGE = 'two_person: the activator must differ from the approver';
  END IF;
  WITH r AS (
    UPDATE noema.calibrator o SET status = 'retired', retired_at = now(), retired_reason = 'replaced'
     WHERE o.status = 'active' AND o.scope = v_c.scope AND o.tenant_id IS NOT DISTINCT FROM v_c.tenant_id
       AND o.profile_id = v_c.profile_id AND o.profile_version = v_c.profile_version
       AND o.question_signature = v_c.question_signature AND o.member_subset = v_c.member_subset
       AND o.input_view_signature = v_c.input_view_signature AND o.choice_method = v_c.choice_method
    RETURNING o.id)
  SELECT coalesce(array_agg(r.id), '{}') INTO v_replaced FROM r;
  UPDATE noema.calibrator c
     SET status = 'active', activated_by = ARRAY[v_c.approved_by, p_operator_id], activated_at = now()
   WHERE c.id = p_calibrator_id;
  PERFORM admin._bump(admin._scope_key(v_c.scope, v_c.tenant_id));
  PERFORM admin._audit(v_c.tenant_id, 'operator', p_operator_id, 'calibrator.activate', 'calibrator', p_calibrator_id,
                       jsonb_build_object('replaced', to_jsonb(v_replaced)));
END $$;

CREATE FUNCTION admin.calibrator_retire(p_operator_id text, p_calibrator_id text, p_reason text DEFAULT 'manual')
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF p_reason IS NULL OR p_reason NOT IN ('manual', 'profile_version_retired') THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: reason is manual or profile_version_retired';
  END IF;
  PERFORM admin._transition(p_operator_id, 'operator', p_calibrator_id, 'retired', p_reason);
END $$;

RESET ROLE;
