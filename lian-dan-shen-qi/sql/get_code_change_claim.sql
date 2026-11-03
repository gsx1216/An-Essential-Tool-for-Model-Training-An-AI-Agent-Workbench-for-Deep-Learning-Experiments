SELECT change_id FROM experiment_assistant.code_change_claims WHERE session_id=%s AND turn_id=%s FOR UPDATE;
