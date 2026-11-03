ALTER TABLE experiment_assistant.chat_turns ADD UNIQUE INDEX uq_chat_tool_turn_session (session_id,id);
