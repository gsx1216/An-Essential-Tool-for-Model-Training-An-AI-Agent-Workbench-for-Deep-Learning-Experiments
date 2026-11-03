SELECT attempt_token,call_index,tool_name,arguments_json,result_json,created_at
FROM experiment_assistant.suggestion_evidence WHERE task_id=%s
ORDER BY created_at DESC,call_index DESC LIMIT 24;
