SELECT CHECK_CLAUSE FROM information_schema.check_constraints
WHERE constraint_schema='experiment_assistant' AND constraint_name='ck_servers_secret';
