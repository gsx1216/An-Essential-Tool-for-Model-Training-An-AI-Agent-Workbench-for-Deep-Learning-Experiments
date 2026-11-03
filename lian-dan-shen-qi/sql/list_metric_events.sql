SELECT source_offset, step, epoch, split, dataset, values_json FROM experiment_assistant.run_metric_events
WHERE user_id = %s AND run_id = %s AND source_offset > %s
  AND (%s IS NULL OR split = %s)
  AND (%s IS NULL OR JSON_CONTAINS_PATH(values_json, 'one', CONCAT('$.', %s)))
AND (%s IS NULL OR dataset = %s)
ORDER BY source_offset LIMIT %s;
