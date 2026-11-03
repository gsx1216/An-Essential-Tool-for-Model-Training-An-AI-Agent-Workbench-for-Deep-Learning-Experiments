UPDATE experiment_assistant.research_rate_limits SET next_allowed=TIMESTAMPADD(SECOND,3,UTC_TIMESTAMP(6)) WHERE provider='public_research';
