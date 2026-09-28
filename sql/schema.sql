-- Pillar store: one JSON object per row, merged in order.
--   target = 'default'   -> applies to every minion
--   target = <minion_id> -> applies to that minion only
-- Rows are merged defaults first, then by ascending priority (later wins).

CREATE TABLE IF NOT EXISTS pillar_data (
    id         bigserial   PRIMARY KEY,
    target     text        NOT NULL,
    priority   integer     NOT NULL DEFAULT 100,
    data       jsonb       NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pillar_data_target_idx ON pillar_data (target, priority);

-- Read-only role used by the Salt master.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'salt_pillar') THEN
        CREATE ROLE salt_pillar LOGIN PASSWORD 'salt_pillar';
    END IF;
END
$$;

GRANT SELECT ON pillar_data TO salt_pillar;
