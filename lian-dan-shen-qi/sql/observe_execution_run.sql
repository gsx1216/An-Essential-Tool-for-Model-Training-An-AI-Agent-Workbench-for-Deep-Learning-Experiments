UPDATE experiment_assistant.experiment_runs
SET status = %s,
    started_at = CASE WHEN %s THEN COALESCE(started_at, UTC_TIMESTAMP(6)) ELSE started_at END,
    finished_at = CASE WHEN %s IN ('succeeded', 'failed', 'cancelled') THEN UTC_TIMESTAMP(6) ELSE NULL END,
    exit_code = CASE WHEN %s IN ('succeeded', 'failed', 'cancelled') THEN %s ELSE NULL END,
    error_code = %s,
    log_ref = CASE WHEN %s > 0 THEN %s ELSE log_ref END,
    updated_at = UTC_TIMESTAMP(6)
WHERE user_id = %s AND id = %s AND status IN ('queued', 'running')
  AND NOT (status = 'running' AND %s = 'queued');
