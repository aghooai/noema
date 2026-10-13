-- 0002_c2a_requests / 20: noema-admin functions of the C2a requests (spec-03 §4; IMPL-008, IMPL-044). Operator-checked
-- like every admin.* function; writes are audited (tenant '__platform__').
SET LOCAL ROLE noema_definer;

-- identity 1: the stored body of one config version, NULL for an unknown version (noema-admin config approve retires
-- the calibrators whose profile version leaves the approved config, ADR-003 §5; noema_cli has no table privileges).
CREATE FUNCTION admin.config_body(p_operator_id text, p_version int) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  RETURN (SELECT c.body FROM platform.config_version c WHERE c.version = p_version);
END $$;

-- identity 5: pepper rotation for operator keys (auth.rehash_api_key does it for API keys): replaces the calling
-- operator's key_hash only for a pepper version newer than the stored hash's v<p>, never a downgrade. p_hash must be
-- hmac-sha256:v<p_version>:<hex>. True iff updated.
CREATE FUNCTION admin.rehash_operator(p_operator_id text, p_hash text, p_version int) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_rows int;
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF p_hash IS NULL OR p_version IS NULL OR p_hash !~ '^hmac-sha256:v[0-9]+:[0-9a-f]{64}$'
     OR substring(p_hash FROM '^hmac-sha256:v([0-9]+):') <> p_version::text THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: key_hash must be hmac-sha256:v<p_version>:<hex>';
  END IF;
  UPDATE platform.operator o SET key_hash = p_hash
   WHERE o.id = p_operator_id AND o.revoked_at IS NULL
     AND p_version > substring(o.key_hash FROM '^hmac-sha256:v([0-9]+):')::numeric;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows > 0 THEN
    PERFORM admin._audit(NULL, 'operator', p_operator_id, 'operator.rehash', 'operator', p_operator_id,
                         jsonb_build_object('pepper_version', p_version));
  END IF;
  RETURN v_rows > 0;
END $$;

RESET ROLE;
