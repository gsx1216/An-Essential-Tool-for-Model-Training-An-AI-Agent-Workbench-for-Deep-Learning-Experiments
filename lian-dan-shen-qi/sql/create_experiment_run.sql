INSERT INTO experiment_assistant.experiment_runs
    (id, experiment_id, user_id, request_id, input_digest,
     experiment_title, experiment_kind, experiment_description,
     parameters_json, status, created_at, updated_at)
SELECT %s, id, user_id, %s, %s, title, kind, description, %s,
       'created', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
FROM experiment_assistant.experiments
WHERE user_id = %s AND id = %s;
