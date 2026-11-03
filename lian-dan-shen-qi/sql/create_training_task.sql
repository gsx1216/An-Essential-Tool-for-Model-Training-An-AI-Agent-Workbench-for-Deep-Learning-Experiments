INSERT INTO experiment_assistant.training_tasks
 (id,user_id,name,experiment_id,server_id,script_id,configuration_json,created_at)
SELECT %s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6)
FROM experiment_assistant.server_connections s WHERE s.id=%s AND s.user_id=%s AND s.deleted_at IS NULL;
