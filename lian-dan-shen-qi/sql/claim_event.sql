UPDATE experiment_assistant.event_outbox SET status='sending',attempts=attempts+1,
 lease_token=%s,lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)) WHERE id=%s;
