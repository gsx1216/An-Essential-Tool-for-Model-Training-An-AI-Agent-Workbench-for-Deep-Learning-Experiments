SELECT e.id, s.spec_json AS evaluation_json, s.spec_digest AS evaluation_digest
FROM experiment_assistant.experiments e
LEFT JOIN experiment_assistant.experiment_evaluation_specs s ON s.experiment_id = e.id AND s.user_id = e.user_id
WHERE e.user_id = %s AND e.id = %s FOR SHARE;
