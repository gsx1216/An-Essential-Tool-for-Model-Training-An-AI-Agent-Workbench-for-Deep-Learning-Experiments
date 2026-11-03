UPDATE experiment_assistant.code_changes SET status=%s,lease_token=%s,lease_until=TIMESTAMPADD(SECOND,300,UTC_TIMESTAMP(6)),error_code=NULL,updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND user_id=%s;
