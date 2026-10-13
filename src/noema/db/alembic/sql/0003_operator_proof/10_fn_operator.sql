-- 0003_operator_proof / 10 (Wave E): the operator check proves the operator key to the database.
-- Before, admin._operator only checked that p_operator_id named a live operator; the key was verified inside the
-- noema-admin client only, so anyone with the shared noema_cli DSN could propose as operator A and approve as B
-- (operator ids are readable in audit rows and list_config_versions). Now the connection must carry the proof in its
-- session setting noema.operator_key, a JSON object {"v": pepper version, "p": pepper, "k": key_id, "s": secret}
-- that the CLI sets after its own check: key_hash = hmac-sha256:v<v>:hex(HMAC-SHA256(p, k || '.' || s)) of the
-- live operator p_operator_id. A session setting is private to its connection, so one connection acts only as the
-- operator whose secret it holds. No table or granted function changes (IMPL-010, spec-03 §4).
-- As in 0001/40: created by noema_definer, which holds CREATE on admin only during this revision.
GRANT CREATE ON SCHEMA admin TO noema_definer;
SET LOCAL ROLE noema_definer;

-- HMAC-SHA256 (RFC 2104) on the built-in sha256(): the IMPL-008 key hash without pgcrypto.
CREATE FUNCTION admin._hmac_sha256(p_key bytea, p_msg bytea) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_key bytea := p_key;
  v_inner bytea;
  v_outer bytea;
BEGIN
  IF length(v_key) > 64 THEN
    v_key := sha256(v_key);
  END IF;
  v_key := v_key || decode(repeat('00', 64 - length(v_key)), 'hex');
  v_inner := v_key;
  v_outer := v_key;
  FOR i IN 0..63 LOOP
    v_inner := set_byte(v_inner, i, get_byte(v_key, i) # 54);   -- 0x36
    v_outer := set_byte(v_outer, i, get_byte(v_key, i) # 92);   -- 0x5c
  END LOOP;
  RETURN sha256(v_outer || sha256(v_inner || p_msg));
END $$;

-- Raises NOE10 unless p_operator_id is a live (not revoked) operator whose key this connection proved.
CREATE OR REPLACE FUNCTION admin._operator(p_operator_id text) RETURNS void
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_proof jsonb;
  v_key_id text;
  v_hash text;
BEGIN
  BEGIN
    v_proof := nullif(current_setting('noema.operator_key', true), '')::jsonb;
  EXCEPTION WHEN others THEN
    v_proof := NULL;
  END;
  SELECT o.key_id, o.key_hash INTO v_key_id, v_hash FROM platform.operator o
   WHERE o.id = p_operator_id AND o.revoked_at IS NULL;
  IF v_hash IS NULL OR jsonb_typeof(v_proof) IS DISTINCT FROM 'object'
     OR v_proof ->> 'k' IS DISTINCT FROM v_key_id OR v_proof ->> 'p' IS NULL OR v_proof ->> 's' IS NULL
     OR v_hash IS DISTINCT FROM 'hmac-sha256:v' || (v_proof ->> 'v') || ':' || encode(admin._hmac_sha256(
          convert_to(v_proof ->> 'p', 'UTF8'), convert_to(v_key_id || '.' || (v_proof ->> 's'), 'UTF8')), 'hex') THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE10', MESSAGE = 'operator_invalid: unknown, revoked or unproven operator';
  END IF;
END $$;

RESET ROLE;
REVOKE CREATE ON SCHEMA admin FROM noema_definer;
