-- 0001_initial / 47: two-person config activation and the price catalog (spec-05 §2-§3; IMPL-019, IMPL-044).
SET LOCAL ROLE noema_definer;

-- Stores a validated config as `proposed`. Its version is the body's config_version and must exceed every stored one.
CREATE FUNCTION admin.config_propose(p_operator_id text, p_digest text, p_body jsonb) RETURNS int
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_version int;
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF p_digest IS NULL OR p_digest !~ '^sha256:[0-9a-f]{64}$' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: digest must be sha256:<hex>';
  END IF;
  IF jsonb_typeof(p_body) IS DISTINCT FROM 'object' OR jsonb_typeof(p_body->'config_version') IS DISTINCT FROM 'number'
     OR (p_body->>'config_version') !~ '^[1-9][0-9]{0,8}$' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: the body needs an integer config_version >= 1';
  END IF;
  v_version := (p_body->>'config_version')::int;
  PERFORM pg_advisory_xact_lock(hashtext('noema.admin.config_version'));
  IF EXISTS (SELECT 1 FROM platform.config_version c WHERE c.version >= v_version) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE15', MESSAGE = 'conflict: config_version must exceed every stored version';
  END IF;
  INSERT INTO platform.config_version (version, digest, body, status, proposed_by)
  VALUES (v_version, p_digest, p_body, 'proposed', p_operator_id);
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'config.propose', 'config_version', v_version::text,
                       jsonb_build_object('version', v_version, 'digest', p_digest));
  RETURN v_version;
END $$;

-- A second operator activates a proposed version: the previous active one is superseded, tenants are synced, and
-- config_changed is notified. P1-P1: a db:<n> prices_ref must exist in platform.price_catalog.
CREATE FUNCTION admin.config_approve(p_operator_id text, p_version int) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_row platform.config_version%ROWTYPE;
  v_ref text;
BEGIN
  PERFORM admin._operator(p_operator_id);
  PERFORM pg_advisory_xact_lock(hashtext('noema.admin.config_version'));
  SELECT * INTO v_row FROM platform.config_version c WHERE c.version = p_version FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: config version';
  END IF;
  IF v_row.status <> 'proposed' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: only a proposed version can be approved';
  END IF;
  IF v_row.proposed_by = p_operator_id THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE11', MESSAGE = 'two_person: the approver must differ from the proposer';
  END IF;
  v_ref := v_row.body #>> '{platform,cost,prices_ref}';
  IF v_ref LIKE 'db:%' AND (v_ref !~ '^db:[0-9]{1,9}$' OR NOT EXISTS (
       SELECT 1 FROM platform.price_catalog p WHERE p.version = substr(v_ref, 4)::int)) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: prices_ref is not in platform.price_catalog (P1-P1)';
  END IF;
  UPDATE platform.config_version c SET status = 'superseded' WHERE c.status = 'active';
  UPDATE platform.config_version c
     SET status = 'active', approved_by = p_operator_id, approved_at = now(), activated_at = now()
   WHERE c.version = p_version;
  PERFORM admin._sync_tenants();
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'config.approve', 'config_version', p_version::text,
                       jsonb_build_object('version', p_version, 'digest', v_row.digest));
  PERFORM admin._notify('config_changed', jsonb_build_object('version', p_version));
END $$;

-- Re-proposes a superseded version; a second operator then activates it with config_approve (same two-person rule).
CREATE FUNCTION admin.config_rollback(p_operator_id text, p_version int) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_status text;
BEGIN
  PERFORM admin._operator(p_operator_id);
  PERFORM pg_advisory_xact_lock(hashtext('noema.admin.config_version'));
  SELECT c.status INTO v_status FROM platform.config_version c WHERE c.version = p_version FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: config version';
  END IF;
  IF v_status <> 'superseded' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE13', MESSAGE = 'invalid_state: only a superseded version can be rolled back to';
  END IF;
  UPDATE platform.config_version c
     SET status = 'proposed', proposed_by = p_operator_id, proposed_at = now(), approved_by = NULL,
         approved_at = NULL, activated_at = NULL
   WHERE c.version = p_version;
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'config.rollback', 'config_version', p_version::text,
                       jsonb_build_object('version', p_version));
END $$;

CREATE FUNCTION admin.list_config_versions(p_operator_id text)
RETURNS TABLE (version int, digest text, status text, proposed_by text, proposed_at timestamptz, approved_by text,
               approved_at timestamptz, activated_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  RETURN QUERY
    SELECT c.version, c.digest, c.status, c.proposed_by, c.proposed_at, c.approved_by, c.approved_at, c.activated_at
      FROM platform.config_version c ORDER BY c.version DESC;
END $$;

-- Stores a schema-validated prices file as the next platform.price_catalog version (spec-05 §2).
CREATE FUNCTION admin.import_prices(p_operator_id text, p_digest text, p_body jsonb) RETURNS int
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_version int;
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF p_digest IS NULL OR p_digest !~ '^sha256:[0-9a-f]{64}$' OR jsonb_typeof(p_body) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: digest sha256:<hex> and an object body';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtext('noema.admin.price_catalog'));
  SELECT coalesce(max(p.version), 0) + 1 INTO v_version FROM platform.price_catalog p;
  INSERT INTO platform.price_catalog (version, digest, body, imported_by) VALUES (v_version, p_digest, p_body, p_operator_id);
  PERFORM admin._audit(NULL, 'operator', p_operator_id, 'prices.import', 'price_catalog', v_version::text,
                       jsonb_build_object('version', v_version, 'digest', p_digest));
  RETURN v_version;
END $$;

RESET ROLE;
