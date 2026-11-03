INSERT INTO experiment_assistant.chat_training_plans
 (id,user_id,session_id,prepared_turn_id,task_id,snapshot_json,snapshot_sha256,run_request_id,created_at)
 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6));
