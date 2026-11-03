-- 只读取摘要版本和边界；命中缓存时不重复拉取摘要正文。
SELECT c.version, c.summarized_until_seq
FROM experiment_assistant.chat_context_summaries AS c
JOIN experiment_assistant.chat_sessions AS s ON s.id = c.session_id
WHERE s.user_id = %s AND s.id = %s
FOR UPDATE;
