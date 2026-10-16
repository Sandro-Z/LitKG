CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    title TEXT,
    doi TEXT,
    abbreviation_map JSONB NOT NULL DEFAULT '{}'::jsonb,
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
    page_start INT,
    page_end INT,
    page_spans JSONB NOT NULL DEFAULT '[]'::jsonb,
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

CREATE TABLE IF NOT EXISTS entity_identifiers (
    entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_family TEXT NOT NULL,
    normalized_id TEXT NOT NULL,
    identifier_key TEXT NOT NULL,
    PRIMARY KEY (entity_family, identifier_key),
    UNIQUE (entity_id, identifier_key)
);

-- Retain existing identifiers; canonical prefix/URL variants are added during
-- re-normalization. Chemical IDs are shared across Drug/Compound labels.
INSERT INTO entity_identifiers (entity_id, entity_family, normalized_id, identifier_key)
SELECT id,
       CASE WHEN entity_type IN ('Drug', 'Compound') THEN 'Chemical' ELSE entity_type END,
       normalized_id, LOWER(TRIM(normalized_id))
FROM entities
WHERE normalized_id IS NOT NULL AND TRIM(normalized_id) <> ''
ORDER BY id
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS graph_rebuild_jobs (
    id UUID PRIMARY KEY,
    knowledge_base_id BIGINT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doi TEXT;
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS abbreviation_map JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS page_start INT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS page_end INT;
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS page_spans JSONB NOT NULL DEFAULT '[]'::jsonb;

-- The old pypdf fallback used an unambiguous, one-based PDF page heading.
UPDATE chunks
SET page_start = SUBSTRING(section_title FROM '^Page ([0-9]+)$')::INT,
    page_end = SUBSTRING(section_title FROM '^Page ([0-9]+)$')::INT,
    page_spans = jsonb_build_array(jsonb_build_object(
        'start', 0,
        'end', LENGTH(text),
        'page_start', SUBSTRING(section_title FROM '^Page ([0-9]+)$')::INT,
        'page_end', SUBSTRING(section_title FROM '^Page ([0-9]+)$')::INT
    ))
WHERE page_start IS NULL AND section_title ~ '^Page [1-9][0-9]*$';

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
  AND NOT EXISTS (SELECT 1 FROM knowledge_base_documents member WHERE member.document_id = d.id)
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
CREATE INDEX IF NOT EXISTS idx_entity_identifiers_entity
    ON entity_identifiers(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_identifiers_id_trgm
    ON entity_identifiers USING GIN(normalized_id gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_graph_rebuild_jobs_kb_created
    ON graph_rebuild_jobs(knowledge_base_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_relations_subject
    ON relations(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_object
    ON relations(object_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_predicate
    ON relations(predicate);
CREATE INDEX IF NOT EXISTS idx_relation_evidence_claim
    ON relation_evidence(claim_id);

-- v3: immutable processing versions and source-preserving projections.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS pipeline_generation INT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS extraction_epoch INT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS catalog_version TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS catalog_status TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS catalog_error TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS extraction_version TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS catalog_version TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS processing_version TEXT;

CREATE TABLE IF NOT EXISTS document_catalogs (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, version)
);
CREATE TABLE IF NOT EXISTS document_entities (
    document_id BIGINT NOT NULL,
    catalog_version TEXT NOT NULL,
    local_id TEXT NOT NULL,
    entity_id BIGINT REFERENCES entities(id) ON DELETE RESTRICT,
    entry JSONB NOT NULL,
    PRIMARY KEY (document_id, catalog_version, local_id),
    FOREIGN KEY (document_id, catalog_version) REFERENCES document_catalogs(document_id, version) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS processing_cache (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, stage, cache_key)
);
CREATE TABLE IF NOT EXISTS processing_attempts (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id BIGINT,
    stage TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    output JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS claim_history (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    original_claim_id BIGINT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (original_claim_id, snapshot_hash)
);

-- A fact is a source assertion. Equivalent assertions can share fact_key and
-- the existing deduplicated binary relation, without collapsing their sources.
CREATE TABLE IF NOT EXISTS facts (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT UNIQUE NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation_id BIGINT REFERENCES relations(id) ON DELETE SET NULL,
    fact_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    qualifiers JSONB NOT NULL,
    negated BOOLEAN NOT NULL,
    speculative BOOLEAN NOT NULL,
    catalog_version TEXT,
    processing_version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fact_participants (
    fact_id BIGINT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    mention TEXT,
    PRIMARY KEY (fact_id, role, entity_id)
);
CREATE TABLE IF NOT EXISTS fact_evidence (
    fact_id BIGINT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    ordinal INT NOT NULL,
    evidence JSONB NOT NULL,
    PRIMARY KEY (fact_id, ordinal)
);
CREATE TABLE IF NOT EXISTS review_items (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    item_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version, item_key)
);
CREATE TABLE IF NOT EXISTS derived_content (
    knowledge_base_id BIGINT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    sources JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    embedding vector,
    embedding_model TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (knowledge_base_id, kind, source_id)
);
CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id UUID PRIMARY KEY,
    knowledge_base_id BIGINT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result JSONB,
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS derived_dirty_documents (
    document_id BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    revision BIGINT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_document_entities_entity ON document_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(fact_key);
CREATE INDEX IF NOT EXISTS idx_facts_predicate_polarity ON facts(predicate, negated);
CREATE INDEX IF NOT EXISTS idx_fact_participants_entity ON fact_participants(entity_id, fact_id);
CREATE INDEX IF NOT EXISTS idx_derived_text ON derived_content USING GIN(to_tsvector('simple', content));
CREATE INDEX IF NOT EXISTS idx_derived_substring ON derived_content USING GIN(content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_review_pending ON review_items(document_id, status);
