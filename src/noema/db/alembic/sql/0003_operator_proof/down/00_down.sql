-- 0003_operator_proof downgrade (CI/test only): back to the 0002 objects (the id-only operator check).
GRANT CREATE ON SCHEMA admin TO noema_definer;
SET LOCAL ROLE noema_definer;
CREATE OR REPLACE FUNCTION admin._operator(p_operator_id text) RETURNS void
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p_operator_id IS NULL OR NOT EXISTS (
       SELECT 1 FROM platform.operator o WHERE o.id = p_operator_id AND o.revoked_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE10', MESSAGE = 'operator_invalid: unknown or revoked operator';
  END IF;
END $$;
DROP FUNCTION admin._hmac_sha256(bytea, bytea);
RESET ROLE;
REVOKE CREATE ON SCHEMA admin FROM noema_definer;
