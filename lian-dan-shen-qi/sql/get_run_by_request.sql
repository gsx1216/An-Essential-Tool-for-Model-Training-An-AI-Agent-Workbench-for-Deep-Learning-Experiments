SELECT r.*, r.id AS run_id, s.spec_json AS evaluation_json, s.spec_digest AS evaluation_digest
FROM experiment_assistant.experiment_runs AS r
JOIN experiment_assistant.experiments AS e ON e.id = r.experiment_id AND e.user_id = r.user_id
LEFT JOIN experiment_assistant.run_evaluation_specs s ON s.run_id = r.id AND s.user_id = r.user_id
WHERE r.user_id = %s AND r.request_id = %s;
