-- 0001_initial / 30: schema and table privileges (spec-03 §1). Nothing is granted to PUBLIC.
-- USAGE on auth, ops and admin lets a role resolve the functions it may EXECUTE; it grants no table access.

-- noema_app: the API and worker processes (NOBYPASSRLS; no CREATE anywhere)
GRANT USAGE ON SCHEMA noema, jobs, platform, auth, ops, admin TO noema_app;
GRANT SELECT ON noema.tenant, noema.project, noema.principal, noema.calibrator_epoch TO noema_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON noema.session, noema.turn, noema.turn_file, noema.attempt, noema.decision,
  noema.file, noema.derivative, noema.idempotency_record, noema.labeling_run, noema.label_item TO noema_app;
GRANT SELECT, INSERT, UPDATE ON noema.calibrator TO noema_app;                -- tenant rows only (RLS)
GRANT SELECT ON noema.usage_line TO noema_app;                                -- written by ops.outbox_apply
GRANT SELECT, INSERT ON noema.audit_event TO noema_app;                       -- append-only
REVOKE UPDATE, DELETE ON noema.audit_event FROM noema_app;
GRANT SELECT, INSERT ON jobs.job TO noema_app;                                -- claim/finish/extend via jobs.*
GRANT SELECT ON platform.config_version, platform.price_catalog TO noema_app;
-- none: auth.api_key, platform.operator, ops.outbox_dead_letter

-- noema_cli: EXECUTE on admin.* and auth.lookup_operator only (granted with the functions); no table privileges
GRANT USAGE ON SCHEMA admin, auth TO noema_cli;

-- noema_definer (NOLOGIN, BYPASSRLS): exactly the table privileges its functions use (spec-03 §4)
GRANT USAGE ON SCHEMA noema, auth, jobs, ops, admin, platform TO noema_definer;
GRANT SELECT, INSERT, UPDATE ON platform.config_version, platform.operator TO noema_definer;
GRANT SELECT, INSERT ON platform.price_catalog TO noema_definer;
GRANT SELECT, INSERT, UPDATE ON noema.tenant, auth.api_key, noema.calibrator, noema.calibrator_epoch TO noema_definer;
-- (INSERT ... ON CONFLICT needs SELECT on the arbiter columns, hence SELECT next to every INSERT)
GRANT SELECT, INSERT ON noema.project, noema.principal, noema.turn, noema.decision, noema.turn_file, noema.attempt,
  noema.usage_line, noema.audit_event, ops.outbox_dead_letter TO noema_definer;
GRANT SELECT, UPDATE ON noema.session TO noema_definer;
GRANT SELECT ON noema.file, noema.idempotency_record TO noema_definer;
GRANT SELECT, INSERT, UPDATE, DELETE ON jobs.job TO noema_definer;
