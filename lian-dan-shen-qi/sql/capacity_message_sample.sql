-- 精确统计有限序号窗口的正文字节；不返回正文，不对整表 COUNT。
SELECT COUNT(*) AS inspected_messages,
       COALESCE(SUM(content_bytes), 0) AS content_bytes,
       MIN(seq) AS first_seq, MAX(seq) AS last_seq
FROM (
    SELECT m.seq, OCTET_LENGTH(m.content) AS content_bytes
    FROM experiment_assistant.chat_messages AS m
    JOIN experiment_assistant.chat_sessions AS s ON s.id = m.session_id
    WHERE s.user_id = %s AND s.id = %s AND m.seq > %s
    ORDER BY m.seq
    LIMIT %s
) AS bounded_messages;
