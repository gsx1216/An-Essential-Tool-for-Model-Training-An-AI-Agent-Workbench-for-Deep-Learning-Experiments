ALTER TABLE experiment_assistant.chat_tool_calls DROP CHECK ck_chat_tool_meta,
ADD CONSTRAINT ck_chat_tool_meta CHECK(JSON_VALID(result_meta_json) AND OCTET_LENGTH(result_meta_json)<=24576);
