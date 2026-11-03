UPDATE experiment_assistant.chat_turns SET skill_snapshot_json=%s WHERE session_id=%s AND id=%s AND status='pending';
