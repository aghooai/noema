-- 0001_initial / 20: row-level security (spec-03 §3; IMPL-009). ENABLE + FORCE on every table of noema, auth and
-- jobs (FORCE binds noema_owner too). platform.* and ops.outbox_dead_letter hold no tenant data and have no RLS.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'noema.tenant', 'noema.project', 'noema.principal', 'auth.api_key', 'noema.session', 'noema.turn',
    'noema.turn_file', 'noema.attempt', 'noema.decision', 'noema.file', 'noema.derivative', 'noema.usage_line',
    'noema.idempotency_record', 'noema.audit_event', 'jobs.job', 'noema.labeling_run', 'noema.label_item',
    'noema.calibrator', 'noema.calibrator_epoch']
  LOOP
    EXECUTE pg_catalog.format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
    EXECUTE pg_catalog.format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
  END LOOP;

  -- tenant_isolation: every RLS table except tenant, calibrator and calibrator_epoch (their policies follow)
  FOREACH t IN ARRAY ARRAY[
    'noema.project', 'noema.principal', 'auth.api_key', 'noema.session', 'noema.turn', 'noema.turn_file',
    'noema.attempt', 'noema.decision', 'noema.file', 'noema.derivative', 'noema.usage_line',
    'noema.idempotency_record', 'noema.audit_event', 'jobs.job', 'noema.labeling_run', 'noema.label_item']
  LOOP
    EXECUTE pg_catalog.format(
      'CREATE POLICY tenant_isolation ON %s'
      ' USING (tenant_id = current_setting(''app.tenant_id'', true))'
      ' WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
  END LOOP;

  -- project_scope (restrictive): '' or unset = tenant-wide (tenant_admin, auditor, tenant_uow)
  FOREACH t IN ARRAY ARRAY[
    'noema.session', 'noema.turn', 'noema.decision', 'noema.file', 'noema.usage_line', 'noema.idempotency_record']
  LOOP
    EXECUTE pg_catalog.format(
      'CREATE POLICY project_scope ON %s AS RESTRICTIVE'
      ' USING (coalesce(current_setting(''app.project_id'', true), '''') = '''''
      ' OR project_id = current_setting(''app.project_id'', true))', t);
  END LOOP;
END $$;

-- Calibrators (IMPL-009 §4): a tenant reads its own rows and the platform rows; writes only its own tenant rows.
CREATE POLICY cal_read ON noema.calibrator FOR SELECT
  USING (tenant_id = current_setting('app.tenant_id', true) OR scope = 'platform');
CREATE POLICY cal_write ON noema.calibrator FOR ALL
  USING (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true));
-- Lifecycle guard (IMPL-072 two-person activation): direct writes may only create 'fitted' rows and move a 'fitted'
-- row to accepted/rejected, never set the approval fields; approve/activate/downgrade/retire go through admin.*.
CREATE POLICY cal_lifecycle_insert ON noema.calibrator AS RESTRICTIVE FOR INSERT
  WITH CHECK (status = 'fitted' AND approved_by IS NULL AND activated_by IS NULL AND activated_at IS NULL);
CREATE POLICY cal_lifecycle_update ON noema.calibrator AS RESTRICTIVE FOR UPDATE
  USING (status = 'fitted')
  WITH CHECK (status IN ('fitted','accepted','rejected') AND approved_by IS NULL AND activated_by IS NULL
              AND activated_at IS NULL);

CREATE POLICY epoch_read ON noema.calibrator_epoch FOR SELECT
  USING (scope_key = 'platform' OR scope_key = current_setting('app.tenant_id', true));
-- no write policy on calibrator_epoch: increments only via admin.bump_epoch and the calibrator definer functions
CREATE POLICY tenant_read ON noema.tenant FOR SELECT USING (id = current_setting('app.tenant_id', true));
-- noema.tenant has no write policy: rows come only from admin.sync_tenants
