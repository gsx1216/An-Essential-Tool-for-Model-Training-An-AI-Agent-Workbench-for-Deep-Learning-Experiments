SELECT r.id AS run_id, r.user_id, r.experiment_id, r.experiment_title, r.experiment_kind, r.parameters_json,
       r.status, r.exit_code, r.error_code, r.started_at, r.finished_at, r.result_version,
       s.spec_json AS evaluation_json, s.spec_digest AS evaluation_digest
FROM experiment_assistant.experiment_runs r
LEFT JOIN experiment_assistant.run_evaluation_specs s ON s.run_id = r.id AND s.user_id = r.user_id
WHERE r.user_id = %s AND r.id = %s FOR UPDATE;
