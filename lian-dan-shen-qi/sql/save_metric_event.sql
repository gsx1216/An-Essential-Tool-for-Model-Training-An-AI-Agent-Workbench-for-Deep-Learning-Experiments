INSERT INTO experiment_assistant.run_metric_events
    (run_id, user_id, source_offset, step, epoch, split, dataset, values_json, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6));
