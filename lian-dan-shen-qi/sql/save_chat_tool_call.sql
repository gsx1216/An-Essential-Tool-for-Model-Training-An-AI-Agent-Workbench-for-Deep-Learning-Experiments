INSERT INTO experiment_assistant.chat_tool_calls
(session_id,user_id,turn_id,attempt_id,call_id,call_index,tool_name,arguments_json,outcome,result_meta_json,duration_ms,created_at)
VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6));
