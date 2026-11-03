SELECT SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = 'experiment_assistant' AND TABLE_NAME = 'experiments'
  AND INDEX_NAME = 'uq_experiments_id_owner'
ORDER BY SEQ_IN_INDEX;
