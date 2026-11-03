SELECT *, (status='sending' AND lease_until>UTC_TIMESTAMP(6)) AS lease_active
FROM experiment_assistant.event_outbox WHERE id=%s;
