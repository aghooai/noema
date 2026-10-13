-- 0001_initial / 45: reaper, retention candidates and the calibrator registry reads (spec-03 §4, spec-11 §6,
-- IMPL-072).
SET LOCAL ROLE noema_definer;

-- Jobs whose lock lapsed more than p_older_than ago go back to queued (dead at max_attempts); then <= 1,000 jobs
-- done for more than 7 days or dead for more than 30 days are deleted. Returns the requeued count.
CREATE FUNCTION ops.reap_stuck(p_older_than interval) RETURNS int
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_requeued int;
BEGIN
  WITH r AS (
    UPDATE jobs.job j
       SET state = CASE WHEN j.attempts >= j.max_attempts THEN 'dead' ELSE 'queued' END,
           locked_by = NULL, locked_until = NULL, last_error = 'lease_expired', updated_at = now()
     WHERE j.state = 'running' AND j.locked_until < now() - p_older_than
    RETURNING j.state)
  SELECT count(*) FILTER (WHERE r.state = 'queued') INTO v_requeued FROM r;

  DELETE FROM jobs.job d
   WHERE d.id IN (SELECT f.id FROM jobs.job f
                   WHERE f.state IN ('done','dead') AND f.updated_at < now() - interval '7 days'
                     AND (f.state = 'done' OR f.updated_at < now() - interval '30 days')
                   LIMIT 1000);
  RETURN v_requeued;
END $$;

-- (tenant_id, kind, id) of expired rows, oldest first; kind = the table name. Each branch uses its *_expiry index.
CREATE FUNCTION ops.retention_candidates(p_limit int)
RETURNS TABLE (tenant_id text, kind text, id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT c.tenant_id, c.kind, c.id FROM (
    (SELECT s.tenant_id, 'session'::text AS kind, s.id, s.expires_at FROM noema.session s
      WHERE s.expires_at < now() ORDER BY s.expires_at LIMIT p_limit)
    UNION ALL
    (SELECT t.tenant_id, 'turn'::text, t.id, t.expires_at FROM noema.turn t
      WHERE t.expires_at < now() ORDER BY t.expires_at LIMIT p_limit)
    UNION ALL
    (SELECT d.tenant_id, 'decision'::text, d.id, d.expires_at FROM noema.decision d
      WHERE d.expires_at < now() ORDER BY d.expires_at LIMIT p_limit)
    UNION ALL
    (SELECT f.tenant_id, 'file'::text, f.id, f.expires_at FROM noema.file f
      WHERE f.expires_at < now() ORDER BY f.expires_at LIMIT p_limit)
    UNION ALL
    (SELECT i.tenant_id, 'idempotency_record'::text, i.id, i.expires_at FROM noema.idempotency_record i
      WHERE i.expires_at < now() ORDER BY i.expires_at LIMIT p_limit)
  ) c
  ORDER BY c.expires_at, c.kind, c.id
  LIMIT p_limit $$;

-- (scope_key, n) for every scope: 'platform' and every live tenant (n = 0 until its first bump).
CREATE FUNCTION ops.calibrator_epochs()
RETURNS TABLE (scope_key text, n bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT e.scope_key, e.n FROM noema.calibrator_epoch e
  UNION ALL
  SELECT 'platform', 0 WHERE NOT EXISTS (SELECT 1 FROM noema.calibrator_epoch e WHERE e.scope_key = 'platform')
  UNION ALL
  SELECT t.id, 0 FROM noema.tenant t
   WHERE t.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM noema.calibrator_epoch e WHERE e.scope_key = t.id) $$;

-- The active artifacts of the given scopes ('platform' = the platform rows): every column except acceptance and
-- reliability.
CREATE FUNCTION ops.active_calibrators(p_scope_keys text[])
RETURNS TABLE (id text, scope text, tenant_id text, profile_id text, profile_version text, question_name text,
               question_signature text, member_subset text[], input_view_signature text, choice_method text,
               method text, member_t jsonb, pooled jsonb, epsilon numeric, holdout jsonb, status text,
               fitted_at timestamptz, accepted_at timestamptz, approved_by text, activated_by text[],
               activated_at timestamptz, downgraded_at timestamptz, downgrade_reason text, retired_at timestamptz,
               retired_reason text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT c.id, c.scope, c.tenant_id, c.profile_id, c.profile_version, c.question_name, c.question_signature,
         c.member_subset, c.input_view_signature, c.choice_method, c.method, c.member_t, c.pooled, c.epsilon,
         c.holdout, c.status, c.fitted_at, c.accepted_at, c.approved_by, c.activated_by, c.activated_at,
         c.downgraded_at, c.downgrade_reason, c.retired_at, c.retired_reason
    FROM noema.calibrator c
   WHERE c.status = 'active'
     AND (CASE WHEN c.scope = 'platform' THEN 'platform' ELSE c.tenant_id END) = ANY (p_scope_keys) $$;

RESET ROLE;
