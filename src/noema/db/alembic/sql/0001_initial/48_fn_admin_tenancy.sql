-- 0001_initial / 48: projects, principals and API keys (spec-03 §4, spec-09 §2; IMPL-008, IMPL-044).
SET LOCAL ROLE noema_definer;

CREATE FUNCTION admin.create_project(p_operator_id text, p_tenant_id text, p_name text,
                                     p_default_data_class text DEFAULT 'internal') RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_id text := 'prj_' || ops.ulid();
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF NOT EXISTS (SELECT 1 FROM noema.tenant t WHERE t.id = p_tenant_id AND t.deleted_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: tenant';
  END IF;
  INSERT INTO noema.project (tenant_id, id, name, default_data_class)
  VALUES (p_tenant_id, v_id, p_name, coalesce(p_default_data_class, 'internal'));
  PERFORM admin._audit(p_tenant_id, 'operator', p_operator_id, 'project.create', 'project', v_id,
                       jsonb_build_object('name', p_name, 'default_data_class', coalesce(p_default_data_class, 'internal')));
  RETURN v_id;
END $$;

CREATE FUNCTION admin.create_principal(p_operator_id text, p_tenant_id text, p_project_id text, p_subject text,
                                       p_roles text[]) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_id text := 'prn_' || ops.ulid();
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF NOT EXISTS (SELECT 1 FROM noema.project j JOIN noema.tenant t ON t.id = j.tenant_id AND t.deleted_at IS NULL
                  WHERE j.tenant_id = p_tenant_id AND j.id = p_project_id AND j.deleted_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: project';
  END IF;
  IF coalesce(cardinality(p_roles), 0) = 0 THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: a principal needs at least one role';
  END IF;
  INSERT INTO noema.principal (tenant_id, project_id, id, subject, roles)
  VALUES (p_tenant_id, p_project_id, v_id, p_subject, p_roles);
  PERFORM admin._audit(p_tenant_id, 'operator', p_operator_id, 'principal.create', 'principal', v_id,
                       jsonb_build_object('project_id', p_project_id, 'roles', to_jsonb(p_roles)));
  RETURN v_id;
END $$;

-- The CLI mints key_id + secret and passes only the HMAC (the pepper never reaches the database).
CREATE FUNCTION admin.create_api_key(p_operator_id text, p_tenant_id text, p_project_id text, p_principal_id text,
                                     p_key_id text, p_key_hash text, p_pepper_version int, p_env text,
                                     p_scopes text[], p_ip_allowlist cidr[] DEFAULT NULL,
                                     p_expires_at timestamptz DEFAULT NULL) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  PERFORM admin._check_key(p_key_id, p_key_hash);
  IF p_pepper_version IS NULL OR p_key_hash NOT LIKE 'hmac-sha256:v' || p_pepper_version::text || ':%' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: key_hash version must equal pepper_version';
  END IF;
  IF coalesce(cardinality(p_scopes), 0) = 0 THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: a key needs at least one scope';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM noema.principal p
                   JOIN noema.project j ON j.tenant_id = p.tenant_id AND j.id = p.project_id AND j.deleted_at IS NULL
                   JOIN noema.tenant t ON t.id = p.tenant_id AND t.deleted_at IS NULL
                  WHERE p.tenant_id = p_tenant_id AND p.id = p_principal_id AND p.project_id = p_project_id
                    AND p.disabled_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: principal in that project';
  END IF;
  INSERT INTO auth.api_key (key_id, tenant_id, project_id, principal_id, key_hash, pepper_version, env, scopes,
                            ip_allowlist, expires_at)
  VALUES (p_key_id, p_tenant_id, p_project_id, p_principal_id, p_key_hash, p_pepper_version, p_env, p_scopes,
          p_ip_allowlist, p_expires_at);
  PERFORM admin._audit(p_tenant_id, 'operator', p_operator_id, 'api_key.create', 'api_key', p_key_id,
                       jsonb_build_object('project_id', p_project_id, 'principal_id', p_principal_id, 'env', p_env,
                                          'scopes', to_jsonb(p_scopes), 'expires_at', p_expires_at));
END $$;

-- Sets revoked_at (kept when already revoked) and notifies keys_revoked so pods evict their LRU entry.
CREATE FUNCTION admin.revoke_api_key(p_operator_id text, p_key_id text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_tenant text;
BEGIN
  PERFORM admin._operator(p_operator_id);
  UPDATE auth.api_key k SET revoked_at = coalesce(k.revoked_at, now()) WHERE k.key_id = p_key_id
  RETURNING k.tenant_id INTO v_tenant;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE12', MESSAGE = 'not_found: api key';
  END IF;
  PERFORM admin._audit(v_tenant, 'operator', p_operator_id, 'api_key.revoke', 'api_key', p_key_id, '{}'::jsonb);
  PERFORM admin._notify('keys_revoked', jsonb_build_object('tenant_id', v_tenant, 'key_id', p_key_id));
END $$;

CREATE FUNCTION admin.list_projects(p_operator_id text, p_tenant_id text)
RETURNS TABLE (tenant_id text, id text, name text, default_data_class text, created_at timestamptz,
               deleted_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  RETURN QUERY
    SELECT j.tenant_id, j.id, j.name, j.default_data_class, j.created_at, j.deleted_at
      FROM noema.project j WHERE j.tenant_id = p_tenant_id ORDER BY j.id;
END $$;

-- Never returns key hashes.
CREATE FUNCTION admin.list_api_keys(p_operator_id text, p_tenant_id text, p_project_id text DEFAULT NULL)
RETURNS TABLE (key_id text, tenant_id text, project_id text, principal_id text, pepper_version int, env text,
               scopes text[], ip_allowlist cidr[], expires_at timestamptz, revoked_at timestamptz,
               created_at timestamptz, last_used_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  RETURN QUERY
    SELECT k.key_id, k.tenant_id, k.project_id, k.principal_id, k.pepper_version, k.env, k.scopes, k.ip_allowlist,
           k.expires_at, k.revoked_at, k.created_at, k.last_used_at
      FROM auth.api_key k
     WHERE k.tenant_id = p_tenant_id AND (p_project_id IS NULL OR k.project_id = p_project_id)
     ORDER BY k.key_id;
END $$;

-- An audit row for a CLI action outside the database (e.g. an ExternalId rotation in Secrets Manager).
CREATE FUNCTION admin.record_audit(p_operator_id text, p_tenant_id text, p_action text, p_resource_kind text,
                                   p_resource_id text, p_details jsonb DEFAULT '{}'::jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM admin._operator(p_operator_id);
  IF coalesce(p_action, '') = '' OR coalesce(p_resource_kind, '') = '' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: action and resource_kind are required';
  END IF;
  RETURN admin._audit(p_tenant_id, 'operator', p_operator_id, p_action, p_resource_kind, p_resource_id, p_details);
END $$;

RESET ROLE;
