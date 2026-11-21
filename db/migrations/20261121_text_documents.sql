-- Plain-text document import (benchmark gold-standard texts).
-- external_id: caller-supplied stable document identifier (e.g. "biored:15485686").
-- source_block_id/source_offset: chunk position inside the original text block
-- (Unicode code points); NULL for PDF chunks.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_id TEXT;
CREATE INDEX IF NOT EXISTS documents_external_id ON documents(external_id) WHERE external_id IS NOT NULL;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source_block_id TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source_offset INT;
