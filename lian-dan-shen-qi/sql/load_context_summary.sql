SELECT c.content, c.summarized_until_seq, c.version, c.missing_ranges
FROM experiment_assistant.chat_context_summaries AS c
JOIN experiment_assistant.chat_sessions AS s ON s.id = c.session_id
WHERE s.user_id = %s AND s.id = %s
FOR UPDATE;
