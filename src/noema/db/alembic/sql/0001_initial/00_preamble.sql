-- 0001_initial / 00: preconditions, database ACL, schemas and default privileges (spec-03 §1, §4, §7).
-- Runs as noema_owner (the database owner). The roles themselves are cluster-wide and created beforehand by
-- noema.db.engine.ensure_roles (a superuser step), because NOSUPERUSER roles cannot grant BYPASSRLS.
DO $$
BEGIN
  IF current_user <> 'noema_owner' THEN
    RAISE EXCEPTION 'noema migrations run as noema_owner (spec-03 §7), not %', current_user;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'noema_definer' AND NOT rolcanlogin AND rolbypassrls)
     OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'noema_app' AND rolcanlogin AND NOT rolbypassrls AND NOT rolsuper)
     OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'noema_cli' AND rolcanlogin AND NOT rolbypassrls AND NOT rolsuper) THEN
    RAISE EXCEPTION 'the noema roles are missing or have wrong attributes; run noema.db.engine.ensure_roles first';
  END IF;
END $$;

-- Database ACL: nobody but the three login roles may connect; nobody gets TEMP (spec-03 §1).
DO $$
BEGIN
  EXECUTE pg_catalog.format('REVOKE ALL ON DATABASE %I FROM PUBLIC', pg_catalog.current_database());
  EXECUTE pg_catalog.format('GRANT CONNECT ON DATABASE %I TO noema_owner, noema_app, noema_cli', pg_catalog.current_database());
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA noema AUTHORIZATION noema_owner;
CREATE SCHEMA auth AUTHORIZATION noema_owner;
CREATE SCHEMA jobs AUTHORIZATION noema_owner;
CREATE SCHEMA ops AUTHORIZATION noema_owner;
CREATE SCHEMA admin AUTHORIZATION noema_owner;
CREATE SCHEMA platform AUTHORIZATION noema_owner;

-- Functions never get EXECUTE for PUBLIC by default, whoever of the two creates them (spec-03 §4).
ALTER DEFAULT PRIVILEGES FOR ROLE noema_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
SET LOCAL ROLE noema_definer;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
