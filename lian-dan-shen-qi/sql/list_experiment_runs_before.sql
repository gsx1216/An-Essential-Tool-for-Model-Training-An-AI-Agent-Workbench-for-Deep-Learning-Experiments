SELECT id AS run_id, experiment_id, experiment_title, experiment_kind, status,
       created_at, updated_at, started_at, finished_at, exit_code, error_code,
       (log_ref IS NOT NULL) AS has_logs, (result_version > 0) AS has_result, result_version
FROM experiment_assistant.experiment_runs
WHERE user_id = %s AND experiment_id = %s
  AND (created_at < %s OR (created_at = %s AND id < %s))
ORDER BY created_at DESC, id DESC LIMIT %s;
