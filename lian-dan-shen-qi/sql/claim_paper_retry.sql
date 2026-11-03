UPDATE experiment_assistant.papers SET status='processing',lease_token=%s,lease_until=TIMESTAMPADD(SECOND,180,UTC_TIMESTAMP(6)),error_code=NULL,updated_at=UTC_TIMESTAMP(6) WHERE user_id=%s AND id=%s;
