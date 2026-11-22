# Neo4j LLM Graph Builder: schema and driving notes

Pinned release: `neo4j-labs/llm-graph-builder` tag **v0.8.7**, commit `1ade47de63edb161e5ea37a277141643f3af3142`. Everything here comes from reading that commit's source. **It has not been verified against a live database**, because no Docker/Neo4j was available on the machine that wrote this project. Before trusting an export, run the queries at the end against your deployment and update this file with the actual results.

## Requirements (README)

- Neo4j **5.23 or later** with **APOC** (the backend uses `CALL (variable) { ... }`). `baselines/deploy/docker-compose.builder.yml` (started by `scripts/baseline_neo4j_builder.sh`) adds `neo4j:5.26-community` with APOC, because the builder's own compose file has no Neo4j service.

## Backend routes used (`backend/score.py`)

| Route | Form fields used by `kgb` |
|---|---|
| `POST /upload` | `file`, `chunkNumber=1`, `totalChunks=1`, `originalname`, `model`, `uploadId`, plus `uri`, `userName`, `password` (**base64**: `user_credential._decode_password`), `database` |
| `POST /extract` | `source_type="local file"`, `file_name`, `model`, `allowedNodes` (comma list), `allowedRelationship` (comma list of `Source,REL,Target` triples; must be a multiple of 3, and endpoints must be in `allowedNodes`), `token_chunk_size`, `chunk_overlap`, `chunks_to_combine`, plus credentials |
| `GET /document_status/{file_name}` | uses session-cached credentials; `kgb` instead polls `Document.status` directly through the driver |

`/post_processing` (KNN similarity, full-text index, entity embeddings, communities, duplicate-node merging via `/get_duplicate_nodes` / `/merge_duplicate_nodes`) is **never called** by `kgb`. These post-processing steps only run when that route is invoked (or when the UI triggers them), so all baseline graphs are pre-post-processing graphs. Embeddings are the local default `sentence-transformer` / `all-MiniLM-L6-v2` (set in the compose file).

## LLM configuration (`backend/src/llm.py`)

- The model key is upper-cased (`.`→`_`) and read from `LLM_MODEL_CONFIG_<KEY>`. Keys containing `OPENAI`, `AZURE`, `GEMINI`, `ANTHROPIC`, `FIREWORKS`, `GROQ`, `BEDROCK`, `OLLAMA` or `DIFFBOT` select those providers. Any other key uses the generic branch `ChatOpenAI(api_key, base_url=api_endpoint, model, temperature=0)` with value format `model,api_endpoint,api_key`. Use a neutral key such as `LITKG_COMPAT`.
- `temperature=0` is hard-coded. `max_tokens` and `response_format` are **not** configurable through the API (recorded in each graph's `metadata.consistency`).
- Embeddings: default `EMBEDDING_PROVIDER=sentence-transformer`, `EMBEDDING_MODEL=all-MiniLM-L6-v2` (local; no extra API).

## Graph model written by the builder (from `make_relationships.py`, `graphDB_dataAccess.py`)

- `(:Document {fileName, status, model, processingTime, nodeCount, relationshipCount, total_chunks, processed_chunk, errorMessage, ...})`; `status` ends as `Completed` or `Failed` (`main.py`).
- `(:Chunk {id, text, position, length, fileName, content_offset, page_number?})-[:PART_OF]->(:Document)`; `FIRST_CHUNK` / `NEXT_CHUNK` order the chunks.
- `(:Chunk)-[:HAS_ENTITY]->(entity)`; entities carry the `__Entity__` label plus their type label(s), property `id` (the surface name) and optional `description`.
- Relationships between entities use the LLM-chosen type (restricted to `allowedRelationship` in the schema-guided variants).
- Possibly also `SIMILAR` (KNN) and `__Community__` / `IN_COMMUNITY` / `PARENT_COMMUNITY` (communities) after post-processing. `kgb` excludes these structural types.

## Isolation

Entities are merged by `id` across documents in one database, and relationships carry no document provenance. `kgb` therefore requires `neo4j.dedicated_database = true` and clears the database (`MATCH (n) DETACH DELETE n`) before each document. Each export then contains exactly one document's graph. **Never point it at a database that holds other data.**

## Export queries (`kgb/pipelines/neo4j_builder.py`)

```cypher
MATCH (d:Document {fileName: $f}) RETURN d.status AS status, d.errorMessage AS error, d.model AS model
MATCH (d:Document {fileName: $f})<-[:PART_OF]-(c:Chunk) RETURN c.id AS id, c.text AS text, c.position AS position ORDER BY c.position
MATCH (d:Document {fileName: $f})<-[:PART_OF]-(c:Chunk)-[:HAS_ENTITY]->(e) RETURN c.id AS chunk, elementId(e) AS eid, labels(e) AS labels, e.id AS name, e.description AS description
MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids AND NOT type(r) IN $structural RETURN elementId(a) AS a, type(r) AS type, elementId(b) AS b
```

To record the actual schema of your deployment after one document, run:

```cypher
CALL db.labels(); CALL db.relationshipTypes();
MATCH (c:Chunk) RETURN keys(c) LIMIT 1;
MATCH (:Chunk)-[:HAS_ENTITY]->(e) RETURN labels(e), keys(e) LIMIT 5;
```

## Manual fallback (if the API cannot be scripted)

1. Open the builder UI and connect to the dedicated database.
2. In *Graph Schema*, enter node labels = policy node types and relationship types = policy predicate names. This is the schema-guided variant; skip it for no-schema.
3. Upload the document's same-text file from `out/text/` (written by `kgb prep`). Choose the LitKG-equivalent model and click *Generate Graph*.
4. Wait for *Completed*, then export with the Cypher queries above. The scripted `kgb run neo4j` clears the database first, so do not use it to export a graph built by hand.
5. Delete the document's graph before the next document.

## Run-time behaviour that affects a baseline run

- `src/create_chunks.py` splits text with LangChain's `TokenTextSplitter` (tiktoken `gpt2`). tiktoken downloads its BPE files from `openaipublic.blob.core.windows.net` on first use, so in a container without internet every `/extract` fails with the generic `Failed To Process File:… or LLM Unable To Parse Content ` (the exception goes through the generic `except Exception` branch of `score.py`; the trailing space distinguishes it from LLM errors). The generated `Dockerfile.kgb` caches `gpt2`, `cl100k_base` and `o200k_base` under `TIKTOKEN_CACHE_DIR=/opt/kgb/tiktoken` at build time.
- The same file keeps only the first `MAX_TOKEN_CHUNK_SIZE / token_chunk_size` chunks (default 10000 / 200 = 50 chunks, ~10k tokens) for users whose e-mail does not end in `@neo4j.com`. The compose file sets `MAX_TOKEN_CHUNK_SIZE=100000000` so whole papers are extracted (`kgb.pipelines.neo4j_builder.BUILDER_ENV`, part of the checkpoint key).
- The generic OpenAI-compatible branch of `src/llm.py` builds `ChatOpenAI(api_key=<key from LLM_MODEL_CONFIG_*>, …)`. The pinned `openai==2.38.0` client raises `Missing credentials …` for an empty key before any request is sent (langchain-openai passes the empty string through). For a server without authentication the script therefore writes the placeholder key `EMPTY`, which such servers ignore.
- `/extract` takes `embedding_provider` and `embedding_model` from the request form (the builder's frontend always sends them, default `sentence-transformer` / `all-MiniLM-L6-v2`), not from the backend environment. Without them `processing_source()` calls `load_embedding_model(None, None)`, which fails on `None.lower()` before any LLM call. `BuilderClient.extract` sends `BUILDER_EMBEDDING`; the scripts' end-to-end test (`kgb builder-smoke`) sends one small PDF through `/upload` and `/extract` before a run.
- `/extract` reports failures only as a generic message; the real traceback is logged by the backend (`logging.exception('File Failed in extraction: …')`). On failure the backend also deletes the uploaded file, so a retry uploads it again.
- `LLMGraphTransformer` (langchain-neo4j 0.10.0) calls `with_structured_output(...)`, whose default for `ChatOpenAI` is `method="json_schema"`, i.e. the OpenAI `response_format` JSON-schema mode.
