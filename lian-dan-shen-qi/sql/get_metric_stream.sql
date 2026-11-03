SELECT revision, source_offset, state_json, state_digest, updated_at
FROM experiment_assistant.run_metric_streams WHERE user_id = %s AND run_id = %s;
