-- 兼容早期单条保存留下的最后一条 human；只补轮次记录，不改消息和原有 seq。
INSERT INTO experiment_assistant.chat_turns (id, session_id, user_seq, status)
SELECT REPLACE(UUID(), '-', ''), m.session_id, m.seq, 'pending'
FROM experiment_assistant.chat_messages AS m
WHERE m.role = 'human' AND m.seq < 18446744073709551615
  AND NOT EXISTS (
      SELECT 1 FROM experiment_assistant.chat_messages AS newer
      WHERE newer.session_id = m.session_id AND newer.seq > m.seq
  )
  AND NOT EXISTS (
      SELECT 1 FROM experiment_assistant.chat_turns AS existing
      WHERE existing.session_id = m.session_id
        AND (existing.user_seq = m.seq OR existing.status <> 'completed')
  );
