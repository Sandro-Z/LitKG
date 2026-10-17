CREATE TABLE IF NOT EXISTS processing_leases (
    resource TEXT PRIMARY KEY,
    owner UUID NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
