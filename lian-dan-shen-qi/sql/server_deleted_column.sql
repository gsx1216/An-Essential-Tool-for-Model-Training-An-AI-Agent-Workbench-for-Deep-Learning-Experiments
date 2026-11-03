SELECT COUNT(*) FROM information_schema.columns
WHERE table_schema='experiment_assistant' AND table_name='server_connections' AND column_name='deleted_at';
