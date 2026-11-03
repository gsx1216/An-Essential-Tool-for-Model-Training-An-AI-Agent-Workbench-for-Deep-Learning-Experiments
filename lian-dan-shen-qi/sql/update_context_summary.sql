UPDATE experiment_assistant.chat_context_summaries AS c
JOIN experiment_assistant.chat_sessions AS s ON s.id = c.session_id
SET c.content = %s, c.summarized_until_seq = %s, c.version = %s, c.missing_ranges = %s
WHERE s.user_id = %s AND s.id = %s AND c.version = %s
  AND c.summarized_until_seq < %s;
