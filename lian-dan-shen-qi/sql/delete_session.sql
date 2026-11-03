-- 在轮次和消息清理成功后删除会话本身；由仓储在同一事务中提交。
DELETE FROM experiment_assistant.chat_sessions
WHERE user_id = %s
  AND id = %s;
