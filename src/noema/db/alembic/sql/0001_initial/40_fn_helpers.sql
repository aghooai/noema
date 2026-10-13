-- 0001_initial / 40: function ownership setup and internal helpers (spec-03 §4).
-- Every function of 40-48 is created by noema_definer (SET ROLE; noema_owner is a member WITH SET TRUE), so it is
-- owned by it and gets no PUBLIC EXECUTE (default privileges, 00). noema_definer holds CREATE on the function
-- schemas only for the duration of this migration (revoked in 49). Every function sets
-- search_path = pg_catalog, pg_temp (pg_temp last) and names every relation schema-qualified.
-- Custom SQLSTATEs (class NO): NOE01 outbox record error, NOE10 operator_invalid, NOE11 two_person, NOE12 not_found,
-- NOE13 invalid_state, NOE14 invalid_argument, NOE15 conflict, NOE16 forbidden. The message starts with the reason.
GRANT CREATE ON SCHEMA auth, jobs, ops, admin TO noema_definer;
SET LOCAL ROLE noema_definer;

-- A 26-character Crockford base32 ULID (48-bit ms time + 80 random bits), as noema.core.ids.
CREATE FUNCTION ops.ulid() RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_alphabet constant text := '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  v_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint;
  v_a text := replace(gen_random_uuid()::text, '-', '');
  v_b text := replace(gen_random_uuid()::text, '-', '');
  v_bits bit varying;
  v_out text := '';
BEGIN
  -- uuid v4 hex digits 1-12 and 21-32 are random (13 and 17 carry the version and variant)
  v_bits := B'00' || substring(v_ms::bit(64) FROM 17 FOR 48) || ('x' || substr(v_a, 1, 12) || substr(v_b, 21, 8))::bit(80);
  FOR i IN 0..25 LOOP
    v_out := v_out || substr(v_alphabet, substring(v_bits FROM i * 5 + 1 FOR 5)::bit(5)::int + 1, 1);
  END LOOP;
  RETURN v_out;
END $$;

-- Raises NOE10 unless p_operator_id is a live (not revoked) operator.
CREATE FUNCTION admin._operator(p_operator_id text) RETURNS void
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p_operator_id IS NULL OR NOT EXISTS (
       SELECT 1 FROM platform.operator o WHERE o.id = p_operator_id AND o.revoked_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE10', MESSAGE = 'operator_invalid: unknown or revoked operator';
  END IF;
END $$;

-- Appends one audit_event row (never content) and returns its id.
CREATE FUNCTION admin._audit(p_tenant_id text, p_actor_type text, p_actor_id text, p_action text,
                             p_resource_kind text, p_resource_id text, p_details jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_id text := 'aud_' || ops.ulid();
BEGIN
  INSERT INTO noema.audit_event (tenant_id, id, actor_type, actor_id, action, resource_kind, resource_id, details)
  VALUES (coalesce(p_tenant_id, '__platform__'), v_id, p_actor_type, p_actor_id, p_action, p_resource_kind,
          p_resource_id, coalesce(p_details, '{}'::jsonb));
  RETURN v_id;
END $$;

-- pg_notify('noema_events', {channel, payload}); the notify bridge republishes it to Redis (spec-11 §4).
CREATE FUNCTION admin._notify(p_channel text, p_payload jsonb) RETURNS void
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  SELECT pg_catalog.pg_notify('noema_events',
                              pg_catalog.jsonb_build_object('channel', p_channel, 'payload', p_payload)::text) $$;

-- The audit actor of a call that may come from noema_cli (an operator) or noema_app (a labeling job,
-- p_actor = 'system:<purpose>'): returns 'operator' or 'system' after validating the actor.
CREATE FUNCTION admin._actor(p_actor text) RETURNS text
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF session_user = 'noema_app' THEN
    IF p_actor IS NULL OR p_actor NOT LIKE 'system:%' THEN
      RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: a noema_app actor is ''system:<purpose>''';
    END IF;
    RETURN 'system';
  END IF;
  PERFORM admin._operator(p_actor);
  RETURN 'operator';
END $$;

RESET ROLE;
