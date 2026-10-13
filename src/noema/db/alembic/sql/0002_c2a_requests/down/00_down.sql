-- 0002_c2a_requests downgrade (CI/test only): back to the 0001_initial objects.
GRANT CREATE ON SCHEMA jobs TO noema_definer;
SET LOCAL ROLE noema_definer;
DROP FUNCTION admin.rehash_operator(text, text, int);
DROP FUNCTION admin.config_body(text, int);
DROP FUNCTION ops.retention_page(int, timestamptz, text, text, text);
DROP FUNCTION jobs.finish(text, text, boolean, text, boolean);
CREATE FUNCTION jobs.finish(p_id text, p_worker text, p_ok boolean, p_error text, p_permanent boolean)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  WITH u AS (
    UPDATE jobs.job j
       SET state = CASE WHEN p_ok THEN 'done'
                        WHEN coalesce(p_permanent, false) OR j.attempts >= j.max_attempts THEN 'dead'
                        ELSE 'queued' END,
           run_after = CASE WHEN p_ok OR coalesce(p_permanent, false) OR j.attempts >= j.max_attempts THEN j.run_after
                            ELSE now() + interval '30 seconds' * power(2, greatest(j.attempts - 1, 0)) END,
           locked_by = NULL, locked_until = NULL, last_error = left(p_error, 256), updated_at = now()
     WHERE j.id = p_id AND j.state = 'running' AND j.locked_by = p_worker
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM u) $$;
GRANT EXECUTE ON FUNCTION jobs.finish(text, text, boolean, text, boolean) TO noema_app;
RESET ROLE;
REVOKE CREATE ON SCHEMA jobs FROM noema_definer;

ALTER TABLE noema.derivative DROP COLUMN facts;
DROP INDEX noema.label_item_kept;
DROP INDEX noema.labeling_run_by_definition;
DROP INDEX noema.labeling_run_open;
CREATE UNIQUE INDEX labeling_run_open ON noema.labeling_run (tenant_id, profile_id, profile_version, question_name)
  WHERE status = 'running';
