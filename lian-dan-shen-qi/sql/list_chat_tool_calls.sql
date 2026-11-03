SELECT id,turn_id,attempt_id,call_id,call_index,tool_name,arguments_json,outcome,result_meta_json,duration_ms,created_at
FROM experiment_assistant.chat_tool_calls WHERE user_id=%s AND session_id=%s AND id>%s ORDER BY id LIMIT %s;
