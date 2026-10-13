-- 0001_initial / 15: retention rule of spec-03 §5 enforced on every delete path (sweeper, session.delete, repos):
-- deleting a decision keeps its label items for calibration but drops their content excerpt. SECURITY INVOKER: it
-- runs under the deleting transaction's RLS scope (the label items of the same tenant).
CREATE FUNCTION noema.clear_label_excerpts() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  UPDATE noema.label_item li SET content_excerpt = NULL
    FROM gone g
   WHERE li.tenant_id = g.tenant_id AND li.decision_id = g.id AND li.content_excerpt IS NOT NULL;
  RETURN NULL;
END $$;
CREATE TRIGGER decision_clear_label_excerpts AFTER DELETE ON noema.decision
  REFERENCING OLD TABLE AS gone FOR EACH STATEMENT EXECUTE FUNCTION noema.clear_label_excerpts();
