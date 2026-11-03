INSERT INTO experiment_assistant.suggestion_evidence(task_id,attempt_token,call_index,tool_name,arguments_json,result_json,created_at)
VALUES(%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6));
