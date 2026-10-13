-- 0001_initial / 42: the job queue (spec-03 §4, spec-11 §4). Claim/extend/finish are fenced by p_worker: a reaped and
-- re-claimed job is never extended or finished by its old worker.
SET LOCAL ROLE noema_definer;

-- Returns the claimed jobs in claim order (priority, then run_after).
CREATE FUNCTION jobs.claim(p_kinds text[], p_n int, p_worker text, p_lock_s int)
RETURNS TABLE (id text, tenant_id text, kind text, payload jsonb, attempts int)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  WITH claimed AS (
    UPDATE jobs.job j
       SET state = 'running', attempts = j.attempts + 1, locked_by = p_worker,
           locked_until = now() + p_lock_s * interval '1 second', updated_at = now()
     WHERE j.id IN (SELECT q.id FROM jobs.job q
                     WHERE q.state = 'queued' AND q.run_after <= now() AND q.kind = ANY (p_kinds)
                     ORDER BY q.priority, q.run_after
                     FOR UPDATE SKIP LOCKED
                     LIMIT greatest(p_n, 0))
    RETURNING j.id, j.tenant_id, j.kind, j.payload, j.attempts, j.priority, j.run_after)
  SELECT c.id, c.tenant_id, c.kind, c.payload, c.attempts FROM claimed c ORDER BY c.priority, c.run_after, c.id $$;

CREATE FUNCTION jobs.extend(p_id text, p_worker text, p_lock_s int) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  WITH u AS (
    UPDATE jobs.job j SET locked_until = now() + p_lock_s * interval '1 second', updated_at = now()
     WHERE j.id = p_id AND j.state = 'running' AND j.locked_by = p_worker
    RETURNING 1)
  SELECT EXISTS (SELECT 1 FROM u) $$;

-- ok -> done; else dead if p_permanent or attempts >= max_attempts, otherwise queued again after
-- 30 s x 2^(attempts - 1). Clears the lock and sets last_error. True iff this worker still held the job.
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

-- One queued or running '__platform__' job per kind (job_dedupe), due at the next period boundary; several jobs
-- processes may call it every minute (spec-11 §4). True iff a job was inserted.
CREATE FUNCTION jobs.ensure_periodic(p_kind text, p_every interval) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_rows int;
BEGIN
  IF p_kind IS NULL OR p_kind = '' OR p_every IS NULL OR p_every <= interval '0' THEN
    RAISE EXCEPTION USING ERRCODE = 'NOE14', MESSAGE = 'invalid_argument: ensure_periodic needs a kind and a positive interval';
  END IF;
  INSERT INTO jobs.job (id, tenant_id, kind, priority, payload, state, run_after, dedupe_key)
  VALUES ('job_' || ops.ulid(), '__platform__', p_kind, 300, '{}'::jsonb, 'queued',
          date_bin(p_every, now(), timestamptz '2000-01-01 00:00:00+00') + p_every, p_kind)
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows > 0;
END $$;

RESET ROLE;
