-- 0001_initial / 41: pre-tenant key lookups (spec-03 §4; IMPL-008, IMPL-009, IMPL-044).
SET LOCAL ROLE noema_definer;

-- <= 1 row; disabled principals, deleted projects and deleted tenants return no row. revoked_at/expires_at are
-- checked by the caller (spec-09 §2).
CREATE FUNCTION auth.lookup_api_key(p_key_id text)
RETURNS TABLE (tenant_id text, project_id text, principal_id text, key_hash text, pepper_version int, env text,
               scopes text[], roles text[], expires_at timestamptz, revoked_at timestamptz, ip_allowlist cidr[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT k.tenant_id, k.project_id, k.principal_id, k.key_hash, k.pepper_version, k.env, k.scopes, p.roles,
         k.expires_at, k.revoked_at, k.ip_allowlist
    FROM auth.api_key k
    JOIN noema.principal p ON p.tenant_id = k.tenant_id AND p.id = k.principal_id AND p.disabled_at IS NULL
    JOIN noema.project j ON j.tenant_id = k.tenant_id AND j.id = k.project_id AND j.deleted_at IS NULL
    JOIN noema.tenant t ON t.id = k.tenant_id AND t.deleted_at IS NULL
   WHERE k.key_id = p_key_id $$;

-- last_used_at, at most once a minute per key (the authenticator calls it off the request path).
CREATE FUNCTION auth.touch_api_key(p_key_id text) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  UPDATE auth.api_key SET last_used_at = now()
   WHERE key_id = p_key_id AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute') $$;

-- Pepper rotation: replaces the hash only for a newer pepper version (never a downgrade). True iff updated.
CREATE FUNCTION auth.rehash_api_key(p_key_id text, p_hash text, p_pepper_version int) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  WITH u AS (
    UPDATE auth.api_key SET key_hash = p_hash, pepper_version = p_pepper_version
     WHERE key_id = p_key_id AND p_pepper_version > pepper_version
       AND p_hash ~ '^hmac-sha256:v[0-9]+:[0-9a-f]{64}$'
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM u) $$;

-- noema-admin operator authentication (the CLI verifies the HMAC itself).
CREATE FUNCTION auth.lookup_operator(p_key_id text)
RETURNS TABLE (id text, name text, key_hash text, revoked_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT o.id, o.name, o.key_hash, o.revoked_at FROM platform.operator o WHERE o.key_id = p_key_id $$;

RESET ROLE;
