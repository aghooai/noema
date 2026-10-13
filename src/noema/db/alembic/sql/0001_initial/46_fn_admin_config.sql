-- 0001_initial / 46: operators, config activation and tenant sync (spec-03 §4, spec-05 §3; IMPL-044).
-- Every admin.* function takes the authenticated p_operator_id (the CLI verifies the operator key), enforces the
-- two-person rules and writes an audit_event (tenant '__platform__' or the tenant).
SET LOCAL ROLE noema_definer;

-- Key formats of IMPL-008 (operator keys use the same scheme).
CREATE FUNCTION admin._check_key(p_key_id text, p_key_hash text) RETURNS void
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p_key_id IS NULL OR p_key_id !~ '^[0-9A-HJKMNP-TV-Z]{26}$' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: key_id must be a 26-character ULID';
  END IF;
  IF p_key_hash IS NULL OR p_key_hash !~ '^hmac-sha256:v[0-9]+:[0-9a-f]{64}$' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: key_hash must be hmac-sha256:v<p>:<hex>';
  END IF;
END $$;

-- One-time: the first operator. Callable by noema_owner only (IMPL-044: bootstrap needs the owner DSN).
CREATE FUNCTION admin.bootstrap_operator(p_name text, p_key_id text, p_key_hash text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_id text := 'op_' || ops.ulid();
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('noema.admin.operator'));
  IF EXISTS (SELECT 1 FROM platform.operator) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE15', MESSAGE = 'conflict: operators exist already; bootstrap is one-time';
  END IF;
  PERFORM admin._check_key(p_key_id, p_key_hash);
  INSERT INTO platform.operator (id, name, key_id, key_hash) VALUES (v_id, p_name, p_key_id, p_key_hash);
  PERFORM admin._audit(NULL, 'system', 'bootstrap', 'operator.bootstrap', 'operator', v_id,
                       jsonb_build_object('name', p_name));
  RETURN v_id;
END $$;

CREATE FUNCTION admin.create_operator(p_operator_id text, p_name text, p_key_id text, p_key_hash text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_id text := 'op_' || ops.ulid();
BEGIN
  PERFORM admin._operator(p_operator_id);
  PERFORM admin._check_key(p_key_id, p_key_hash);
  INSERT INTO platform.operator (id, name, key_id, key_hash) VALUES (v_id, p_name, p_key_id, p_key_hash);
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'operator.create', 'operator', v_id,
                       jsonb_build_object('name', p_name));
  RETURN v_id;
END $$;

-- Refuses to revoke the last live operator (nobody could administer, and bootstrap is one-time).
CREATE FUNCTION admin.revoke_operator(p_operator_id text, p_target_id text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  PERFORM pg_advisory_xact_lock(hashtext('noema.admin.operator'));
  IF NOT EXISTS (SELECT 1 FROM platform.operator o WHERE o.id = p_target_id) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: operator';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM platform.operator o WHERE o.id = p_target_id AND o.revoked_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: operator already revoked';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM platform.operator o WHERE o.id <> p_target_id AND o.revoked_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: cannot revoke the last live operator';
  END IF;
  UPDATE platform.operator o SET revoked_at = now() WHERE o.id = p_target_id;
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'operator.revoke', 'operator', p_target_id, '{}'::jsonb);
END $$;

-- noema.tenant from the active config: upsert every configured tenant, soft-delete the others. Returns the count of
-- configured tenants.
CREATE FUNCTION admin._sync_tenants() RETURNS int
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_tenants jsonb;
BEGIN
  SELECT coalesce(c.body->'tenants', '{}'::jsonb) INTO v_tenants FROM platform.config_version c WHERE c.status = 'active';
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: no active config version';
  END IF;
  INSERT INTO noema.tenant AS t (id, aws_account_id)
  SELECT e.key, e.value->>'aws_account_id' FROM jsonb_each(v_tenants) e
  ON CONFLICT (id) DO UPDATE SET aws_account_id = EXCLUDED.aws_account_id, deleted_at = NULL;
  UPDATE noema.tenant t SET deleted_at = now() WHERE t.deleted_at IS NULL AND NOT (v_tenants ? t.id);
  RETURN (SELECT count(*) FROM jsonb_object_keys(v_tenants))::int;
END $$;

CREATE FUNCTION admin.sync_tenants(p_operator_id text) RETURNS int
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_n int;
BEGIN
  PERFORM admin._operator(p_operator_id);
  v_n := admin._sync_tenants();
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'tenants.sync', 'tenant', NULL, jsonb_build_object('tenants', v_n));
  RETURN v_n;
END $$;

RESET ROLE;
