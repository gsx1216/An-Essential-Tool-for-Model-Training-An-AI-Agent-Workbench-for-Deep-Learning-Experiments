SELECT id AS reference_id,source_url AS url,title,source_kind AS kind,content_sha256,retrieved_at,run_id
FROM experiment_assistant.research_references WHERE user_id=%s AND session_id=%s ORDER BY retrieved_at DESC,id DESC LIMIT %s;
