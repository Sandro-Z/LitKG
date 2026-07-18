CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    section_title TEXT,
    text TEXT NOT NULL,
    token_estimate INT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_batches (
    id BIGSERIAL PRIMARY KEY,
    knowledge_base_id BIGINT NOT NULL
        REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    submitted_count INT NOT NULL DEFAULT 0,
    accepted_count INT NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_base_documents (
    knowledge_base_id BIGINT NOT NULL
        REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    document_id BIGINT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    added_via_batch_id BIGINT
        REFERENCES ingestion_batches(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (knowledge_base_id, document_id)
);

CREATE TABLE IF NOT EXISTS ingestion_batch_items (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL
        REFERENCES ingestion_batches(id) ON DELETE CASCADE,
    position INT NOT NULL,
    filename TEXT NOT NULL,
    document_id BIGINT
        REFERENCES documents(id) ON DELETE SET NULL,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(batch_id, position)
);

CREATE TABLE IF NOT EXISTS entities (
    id BIGSERIAL PRIMARY KEY,
    canonical_key TEXT UNIQUE NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    normalized_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id BIGSERIAL PRIMARY KEY,
    entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(entity_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS claims (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    subject_text TEXT,
    subject_type TEXT,
    subject_normalized_id TEXT,
    subject_entity_id BIGINT REFERENCES entities(id) ON DELETE RESTRICT,
    predicate TEXT,
    object_text TEXT,
    object_type TEXT,
    object_normalized_id TEXT,
    object_entity_id BIGINT REFERENCES entities(id) ON DELETE RESTRICT,
    evidence_text TEXT NOT NULL,
    confidence REAL,
    negated BOOLEAN NOT NULL DEFAULT false,
    speculative BOOLEAN NOT NULL DEFAULT false,
    claim_fingerprint TEXT,
    claim_json JSONB NOT NULL,
    extraction_model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- These ALTER statements make this script safe to apply to databases created by
-- the original single-document version.
ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS subject_normalized_id TEXT;
ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS subject_entity_id BIGINT;
ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS object_normalized_id TEXT;
ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS object_entity_id BIGINT;
ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS claim_fingerprint TEXT;

DO $$
BEGIN
    ALTER TABLE claims
        ADD CONSTRAINT claims_subject_entity_id_fkey
        FOREIGN KEY (subject_entity_id) REFERENCES entities(id) ON DELETE RESTRICT;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE claims
        ADD CONSTRAINT claims_object_entity_id_fkey
        FOREIGN KEY (object_entity_id) REFERENCES entities(id) ON DELETE RESTRICT;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS relations (
    id BIGSERIAL PRIMARY KEY,
    subject_entity_id BIGINT NOT NULL
        REFERENCES entities(id) ON DELETE RESTRICT,
    predicate TEXT NOT NULL,
    object_entity_id BIGINT NOT NULL
        REFERENCES entities(id) ON DELETE RESTRICT,
    qualifiers JSONB NOT NULL DEFAULT '{}'::jsonb,
    qualifiers_hash TEXT NOT NULL,
    negated BOOLEAN NOT NULL DEFAULT false,
    speculative BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (
        subject_entity_id,
        predicate,
        object_entity_id,
        qualifiers_hash,
        negated,
        speculative
    )
);

CREATE TABLE IF NOT EXISTS relation_evidence (
    relation_id BIGINT NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    claim_id BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (relation_id, claim_id),
    UNIQUE (claim_id)
);

INSERT INTO knowledge_bases (slug, name, description)
VALUES (
    'default',
    'Default knowledge base',
    'Backward-compatible knowledge base for the original /documents endpoint'
)
ON CONFLICT (slug) DO NOTHING;

-- Existing documents become members of the default knowledge base. Their claims
-- can be projected with POST /knowledge-bases/{id}/rebuild-graph.
INSERT INTO knowledge_base_documents (knowledge_base_id, document_id)
SELECT kb.id, d.id
FROM knowledge_bases kb
CROSS JOIN documents d
WHERE kb.slug = 'default'
ON CONFLICT (knowledge_base_id, document_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_documents_status
    ON documents(status);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id
    ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_status
    ON chunks(status);
CREATE INDEX IF NOT EXISTS idx_claims_document_id
    ON claims(document_id);
CREATE INDEX IF NOT EXISTS idx_claims_chunk_id
    ON claims(chunk_id);
CREATE INDEX IF NOT EXISTS idx_claims_predicate
    ON claims(predicate);
CREATE INDEX IF NOT EXISTS idx_claims_subject_entity
    ON claims(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_claims_object_entity
    ON claims(object_entity_id);
CREATE INDEX IF NOT EXISTS idx_claims_json
    ON claims USING GIN(claim_json);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_chunk_fingerprint
    ON claims(chunk_id, claim_fingerprint);
CREATE INDEX IF NOT EXISTS idx_kb_documents_document
    ON knowledge_base_documents(document_id);
CREATE INDEX IF NOT EXISTS idx_batch_items_batch_status
    ON ingestion_batch_items(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_batch_items_document
    ON ingestion_batch_items(document_id);
CREATE INDEX IF NOT EXISTS idx_entities_type_name
    ON entities(entity_type, canonical_name);
CREATE INDEX IF NOT EXISTS idx_entities_name_trgm
    ON entities USING GIN(canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entities_normalized_id_trgm
    ON entities USING GIN(normalized_id gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized
    ON entity_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias_trgm
    ON entity_aliases USING GIN(alias gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_relations_subject
    ON relations(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_object
    ON relations(object_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_predicate
    ON relations(predicate);
CREATE INDEX IF NOT EXISTS idx_relation_evidence_claim
    ON relation_evidence(claim_id);
