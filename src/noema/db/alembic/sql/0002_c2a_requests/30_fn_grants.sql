-- 0002_c2a_requests / 30: EXECUTE grants of the new functions (spec-03 §4 caller column; nothing for PUBLIC, by the
-- default privileges of 0001/00), then noema_definer loses CREATE on the function schemas again.
SET LOCAL ROLE noema_definer;
GRANT EXECUTE ON FUNCTION
  jobs.finish(text, text, boolean, text, boolean), ops.retention_page(int, timestamptz, text, text, text)
TO noema_app;
GRANT EXECUTE ON FUNCTION admin.config_body(text, int), admin.rehash_operator(text, text, int) TO noema_cli;
RESET ROLE;

REVOKE CREATE ON SCHEMA auth, jobs, ops, admin FROM noema_definer;
