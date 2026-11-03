UPDATE experiment_assistant.chat_training_plans SET launch_turn_id=%s
 WHERE user_id=%s AND session_id=%s AND id=%s AND launch_turn_id IS NULL;
