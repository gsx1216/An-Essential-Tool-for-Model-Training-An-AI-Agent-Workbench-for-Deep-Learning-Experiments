WITH expanded AS (
    SELECT e.source_offset, e.step, e.epoch, e.dataset, e.split, k.metric_name,
           JSON_EXTRACT(e.values_json, CONCAT('$.', k.metric_name)) + 0e0 AS metric_value
    FROM experiment_assistant.run_metric_events e,
         JSON_TABLE(JSON_KEYS(e.values_json), '$[*]' COLUMNS(metric_name VARCHAR(64) PATH '$')) k
    WHERE e.user_id = %s AND e.run_id = %s
), ordered AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY dataset, split, metric_name ORDER BY source_offset) AS first_row,
              ROW_NUMBER() OVER (PARTITION BY dataset, split, metric_name ORDER BY source_offset DESC) AS last_row
    FROM expanded
)
SELECT /*+ MAX_EXECUTION_TIME(5000) */ dataset, split, metric_name, COUNT(*) AS points,
       MIN(metric_value) AS min_value, MAX(metric_value) AS max_value,
       MAX(CASE WHEN first_row = 1 THEN metric_value END) AS `first_value`,
       MAX(CASE WHEN last_row = 1 THEN metric_value END) AS `last_value`,
       MAX(CASE WHEN first_row = 1 THEN step END) AS first_step,
       MAX(CASE WHEN last_row = 1 THEN step END) AS last_step,
       MAX(CASE WHEN last_row = 1 THEN epoch END) AS last_epoch,
       MAX(source_offset) AS last_source_offset
FROM ordered GROUP BY dataset, split, metric_name ORDER BY dataset, split, metric_name LIMIT 65;
