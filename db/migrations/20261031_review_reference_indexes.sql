-- Run outside an explicit transaction; existing records are unchanged.
CREATE INDEX CONCURRENTLY IF NOT EXISTS litkg_review_items_claim_ref_idx ON review_items ((detail ->> 'claim_id'));
CREATE INDEX CONCURRENTLY IF NOT EXISTS litkg_review_items_chunk_ref_idx ON review_items ((detail ->> 'chunk_id'));
