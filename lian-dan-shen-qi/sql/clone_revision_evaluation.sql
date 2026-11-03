INSERT INTO experiment_assistant.run_evaluation_specs(run_id,user_id,spec_json,spec_digest)
SELECT %s,user_id,spec_json,spec_digest FROM experiment_assistant.run_evaluation_specs WHERE user_id=%s AND run_id=%s;
