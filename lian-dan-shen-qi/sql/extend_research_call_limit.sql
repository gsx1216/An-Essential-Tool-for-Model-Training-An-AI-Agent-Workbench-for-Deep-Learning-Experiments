ALTER TABLE experiment_assistant.chat_tool_calls DROP CHECK ck_chat_tool_index, ADD CONSTRAINT ck_chat_tool_index CHECK(call_index BETWEEN 1 AND 6);
