-- Stores only explicitly applied scoped repairs. Planning is read-only.
CREATE TABLE IF NOT EXISTS scoped_repair_runs (
    id UUID PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at TIMESTAMPTZ
);
