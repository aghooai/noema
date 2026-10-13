-- 0002_c2a_requests / 10: job and retention functions of the C2a requests (spec-03 §4, spec-11 §4, §6). As in 0001/40:
-- created by noema_definer (its own), which holds CREATE on the function schemas only during this revision (revoked
-- in 30); search_path = pg_catalog, pg_temp; every relation schema-qualified.
GRANT CREATE ON SCHEMA auth, jobs, ops, admin TO noema_definer;
SET LOCAL ROLE noema_definer;

-- ops-ledger 1: as 0001's jobs.finish, but returns the job's new state ('done' | 'queued' | 'dead'); NULL iff
-- p_worker no longer held the job (the worker logs a dead job, spec-11 §4). Same arguments, so old callers that
-- ignore the result keep working.
DROP FUNCTION jobs.finish(text, text, boolean, text, boolean);
CREATE FUNCTION jobs.finish(p_id text, p_worker text, p_ok boolean, p_error text, p_permanent boolean)
RETURNS text
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  UPDATE jobs.job j
     SET state = CASE WHEN p_ok THEN 'done'
                      WHEN coalesce(p_permanent, false) OR j.attempts >= j.max_attempts THEN 'dead'
                      ELSE 'queued' END,
         run_after = CASE WHEN p_ok OR coalesce(p_permanent, false) OR j.attempts >= j.max_attempts THEN j.run_after
                          ELSE now() + interval '30 seconds' * power(2, greatest(j.attempts - 1, 0)) END,
         locked_by = NULL, locked_until = NULL, last_error = left(p_error, 256), updated_at = now()
   WHERE j.id = p_id AND j.state = 'running' AND j.locked_by = p_worker
  RETURNING j.state $$;

-- ops-ledger 5: (tenant_id, kind, id, expires_at) of expired rows in (expires_at, kind, id, tenant_id) order (text in
-- the "C" collation), strictly after the candidate (p_after_*; p_after_at NULL = from the start), so rows that keep
-- failing never hold a page's head. Each branch range-scans its *_expiry index from p_after_at.
CREATE FUNCTION ops.retention_page(p_limit int, p_after_at timestamptz, p_after_kind text, p_after_id text,
                                   p_after_tenant text)
RETURNS TABLE (tenant_id text, kind text, id text, expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT c.tenant_id, c.kind, c.id, c.expires_at FROM (
    (SELECT s.tenant_id, 'session'::text AS kind, s.id, s.expires_at FROM noema.session s
      WHERE s.expires_at < now() AND (p_after_at IS NULL OR (s.expires_at >= p_after_at AND
            (s.expires_at, 'session' COLLATE "C", s.id COLLATE "C", s.tenant_id COLLATE "C")
              > (p_after_at, p_after_kind, p_after_id, p_after_tenant)))
      ORDER BY s.expires_at, s.id COLLATE "C", s.tenant_id COLLATE "C" LIMIT p_limit)
    UNION ALL
    (SELECT t.tenant_id, 'turn'::text, t.id, t.expires_at FROM noema.turn t
      WHERE t.expires_at < now() AND (p_after_at IS NULL OR (t.expires_at >= p_after_at AND
            (t.expires_at, 'turn' COLLATE "C", t.id COLLATE "C", t.tenant_id COLLATE "C")
              > (p_after_at, p_after_kind, p_after_id, p_after_tenant)))
      ORDER BY t.expires_at, t.id COLLATE "C", t.tenant_id COLLATE "C" LIMIT p_limit)
    UNION ALL
    (SELECT d.tenant_id, 'decision'::text, d.id, d.expires_at FROM noema.decision d
      WHERE d.expires_at < now() AND (p_after_at IS NULL OR (d.expires_at >= p_after_at AND
            (d.expires_at, 'decision' COLLATE "C", d.id COLLATE "C", d.tenant_id COLLATE "C")
              > (p_after_at, p_after_kind, p_after_id, p_after_tenant)))
      ORDER BY d.expires_at, d.id COLLATE "C", d.tenant_id COLLATE "C" LIMIT p_limit)
    UNION ALL
    (SELECT f.tenant_id, 'file'::text, f.id, f.expires_at FROM noema.file f
      WHERE f.expires_at < now() AND (p_after_at IS NULL OR (f.expires_at >= p_after_at AND
            (f.expires_at, 'file' COLLATE "C", f.id COLLATE "C", f.tenant_id COLLATE "C")
              > (p_after_at, p_after_kind, p_after_id, p_after_tenant)))
      ORDER BY f.expires_at, f.id COLLATE "C", f.tenant_id COLLATE "C" LIMIT p_limit)
    UNION ALL
    (SELECT i.tenant_id, 'idempotency_record'::text, i.id, i.expires_at FROM noema.idempotency_record i
      WHERE i.expires_at < now() AND (p_after_at IS NULL OR (i.expires_at >= p_after_at AND
            (i.expires_at, 'idempotency_record' COLLATE "C", i.id COLLATE "C", i.tenant_id COLLATE "C")
              > (p_after_at, p_after_kind, p_after_id, p_after_tenant)))
      ORDER BY i.expires_at, i.id COLLATE "C", i.tenant_id COLLATE "C" LIMIT p_limit)
  ) c
  ORDER BY c.expires_at, c.kind COLLATE "C", c.id COLLATE "C", c.tenant_id COLLATE "C"
  LIMIT p_limit $$;

RESET ROLE;
