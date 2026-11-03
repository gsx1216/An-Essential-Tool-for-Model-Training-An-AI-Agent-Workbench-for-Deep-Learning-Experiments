SELECT * FROM experiment_assistant.chat_training_plans
 WHERE user_id=%s AND session_id=%s ORDER BY created_at DESC,id DESC LIMIT 1;
