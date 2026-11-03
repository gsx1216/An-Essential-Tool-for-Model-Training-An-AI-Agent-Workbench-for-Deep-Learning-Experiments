SELECT JSON_UNQUOTE(JSON_EXTRACT(snapshot_json, '$.sha256'))
FROM experiment_assistant.personal_skills
WHERE user_id=%s AND skill_id=%s FOR SHARE;
