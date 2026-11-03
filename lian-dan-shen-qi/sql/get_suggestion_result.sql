SELECT content_json,content_digest FROM experiment_assistant.suggestion_results WHERE task_id=%s FOR UPDATE;
