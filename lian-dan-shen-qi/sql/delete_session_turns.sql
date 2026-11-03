-- 先删除引用用户消息的轮次；所属用户和会话同时匹配才允许删除。
DELETE FROM experiment_assistant.chat_turns
WHERE session_id IN (
    SELECT id
    FROM experiment_assistant.chat_sessions
    WHERE user_id = %s
      AND id = %s
);
