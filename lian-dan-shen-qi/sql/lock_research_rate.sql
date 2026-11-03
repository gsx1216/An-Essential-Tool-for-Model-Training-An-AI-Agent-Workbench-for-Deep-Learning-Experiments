SELECT GREATEST(0,TIMESTAMPDIFF(MICROSECOND,UTC_TIMESTAMP(6),next_allowed))/1000000 FROM experiment_assistant.research_rate_limits WHERE provider='public_research' FOR UPDATE;
