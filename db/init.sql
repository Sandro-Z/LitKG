CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded',
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    section_title TEXT,
    text TEXT NOT NULL,
    token_estimate INT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS claims (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
    subject_text TEXT,
    subject_type TEXT,
    predicate TEXT,
    object_text TEXT,
    object_type TEXT,
    evidence_text TEXT NOT NULL,
    confidence REAL,
    negated BOOLEAN DEFAULT false,
    speculative BOOLEAN DEFAULT false,
    claim_json JSONB NOT NULL,
    extraction_model TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);
CREATE INDEX IF NOT EXISTS idx_claims_document_id ON claims(document_id);
CREATE INDEX IF NOT EXISTS idx_claims_predicate ON claims(predicate);
CREATE INDEX IF NOT EXISTS idx_claims_json ON claims USING GIN(claim_json);
