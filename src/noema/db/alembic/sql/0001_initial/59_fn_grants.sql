-- 0001_initial / 59: EXECUTE grants (spec-03 §4 caller column). Run by the owner noema_definer; nothing for PUBLIC.
-- Functions whose name starts with '_' (and ops.ulid) are internal helpers: no grants at all.
SET LOCAL ROLE noema_definer;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA auth, jobs, ops, admin FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
  auth.lookup_api_key(text), auth.touch_api_key(text), auth.rehash_api_key(text, text, int),
  jobs.claim(text[], int, text, int), jobs.extend(text, text, int), jobs.finish(text, text, boolean, text, boolean),
  jobs.ensure_periodic(text, interval),
  ops.outbox_apply(jsonb), ops.outbox_dead(jsonb, text), ops.reap_stuck(interval), ops.retention_candidates(int),
  ops.calibrator_epochs(), ops.active_calibrators(text[]),
  admin.set_calibrator_status(text, text, text, text), admin.bump_epoch(text, text)
TO noema_app;

GRANT EXECUTE ON FUNCTION
  auth.lookup_operator(text),
  admin.create_operator(text, text, text, text), admin.revoke_operator(text, text),
  admin.config_propose(text, text, jsonb), admin.config_approve(text, int), admin.config_rollback(text, int),
  admin.sync_tenants(text), admin.list_config_versions(text), admin.import_prices(text, text, jsonb),
  admin.create_project(text, text, text, text), admin.create_principal(text, text, text, text, text[]),
  admin.create_api_key(text, text, text, text, text, text, int, text, text[], cidr[], timestamptz),
  admin.revoke_api_key(text, text), admin.list_projects(text, text), admin.list_api_keys(text, text, text),
  admin.record_audit(text, text, text, text, text, jsonb),
  admin.calibrator_approve(text, text), admin.calibrator_activate(text, text), admin.calibrator_retire(text, text, text),
  admin.upsert_platform_calibrator(text, jsonb), admin.set_calibrator_status(text, text, text, text),
  admin.bump_epoch(text, text), admin.list_calibrators(text, text, text)
TO noema_cli;

GRANT EXECUTE ON FUNCTION admin.bootstrap_operator(text, text, text) TO noema_owner;   -- IMPL-044: owner DSN only
RESET ROLE;

REVOKE CREATE ON SCHEMA auth, jobs, ops, admin FROM noema_definer;
