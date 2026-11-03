INSERT INTO experiment_assistant.chat_context_summaries
    (session_id, content, summarized_until_seq, version, missing_ranges)
SELECT s.id, %s, %s, %s, %s
FROM experiment_assistant.chat_sessions AS s
WHERE s.user_id = %s AND s.id = %s;
