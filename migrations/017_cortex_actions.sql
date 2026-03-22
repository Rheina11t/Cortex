CREATE TABLE IF NOT EXISTS cortex_actions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    family_id TEXT NOT NULL,
    action_type TEXT NOT NULL, -- 'query_answered', 'event_created', 'document_stored', 'briefing_sent', 'alert_sent', 'memory_stored', 'school_email_processed'
    subject TEXT, -- brief description of what happened, e.g. "Izzy swimming 14 Apr", "car insurance expiry alert"
    detail JSONB, -- full detail: message sent, event id, document id, etc.
    phone_number TEXT, -- who it was sent to (if applicable)
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS cortex_actions_family_id_idx ON cortex_actions(family_id);
CREATE INDEX IF NOT EXISTS cortex_actions_created_at_idx ON cortex_actions(created_at);
CREATE INDEX IF NOT EXISTS cortex_actions_action_type_idx ON cortex_actions(action_type);
ALTER TABLE cortex_actions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON cortex_actions FOR ALL TO service_role USING (true);
