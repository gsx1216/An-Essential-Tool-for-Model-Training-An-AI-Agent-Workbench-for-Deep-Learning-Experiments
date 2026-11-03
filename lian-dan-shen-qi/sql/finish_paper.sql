UPDATE experiment_assistant.papers SET status='ready',page_count=%s,chunk_count=%s,empty_pages_json=%s,parser_version=%s,error_code=NULL,lease_token=NULL,lease_until=NULL,updated_at=UTC_TIMESTAMP(6)
WHERE user_id=%s AND id=%s AND status='processing' AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6);
