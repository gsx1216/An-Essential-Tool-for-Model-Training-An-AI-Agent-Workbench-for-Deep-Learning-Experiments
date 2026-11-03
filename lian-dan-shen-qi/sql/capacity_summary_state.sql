SELECT summarized_until_seq, version
FROM experiment_assistant.chat_context_summaries
WHERE session_id = %s;
