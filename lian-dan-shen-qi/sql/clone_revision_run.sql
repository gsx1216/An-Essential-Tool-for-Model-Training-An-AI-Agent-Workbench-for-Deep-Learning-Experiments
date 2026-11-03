INSERT INTO experiment_assistant.experiment_runs
(id,experiment_id,user_id,request_id,input_digest,parameters_json,experiment_title,experiment_kind,experiment_description,status,created_at,updated_at)
SELECT %s,experiment_id,user_id,%s,%s,%s,experiment_title,experiment_kind,experiment_description,'created',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)
FROM experiment_assistant.experiment_runs WHERE user_id=%s AND id=%s;
