#!/usr/bin/env bash
# Run from the LitKG checkout: bash litkg_diag.sh [failing_document_id]
# Read-only application/database diagnostics. No model requests or task submissions.
set -uo pipefail
if [[ "${1:-}" != "" && ! "${1}" =~ ^[0-9]+$ ]]; then
  printf 'Usage: bash %s [failing_document_id]\n' "$0" >&2
  exit 2
fi
if [[ ! -f docker-compose.yml ]]; then
  printf 'Run this script from the LitKG project root.\n' >&2
  exit 2
fi
out="litkg-diag-$(date -u +%Y%m%dT%H%M%SZ).log"
{
  printf '\n=== UTC / checked-out revision ===\n'
  date -u
  git rev-parse HEAD || true
  git status --short -- app/app docker-compose.yml scripts/catalog-runtime.sha256 || true
  printf '\n=== Services ===\n'
  docker compose ps || true
  printf '\n=== Runtime source hashes (against local manifest) ===\n'
  for service in api worker_parse worker_extract; do
    printf '\n--- %s ---\n' "$service"
    if [[ -f scripts/catalog-runtime.sha256 ]]; then
      docker compose exec -T -w / "$service" sha256sum -c - < scripts/catalog-runtime.sha256 || true
    else
      docker compose exec -T "$service" sha256sum /app/app/catalog.py /app/app/llm.py /app/app/tasks.py || true
    fi
  done
  printf '\n=== Database / grounded-output evidence (worker_extract runtime) ===\n'
  docker compose exec -T -e DIAG_DOCUMENT_ID="${1:-}" worker_extract python - <<'PY_DIAG'
import json
import os
import re
from app.db import get_conn
from app import catalog, llm
from app.entity_types import TYPE_DEFINITIONS


def emit(label, data):
    print('\n--- ' + label + ' ---', flush=True)
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str), flush=True)

requested = os.getenv('DIAG_DOCUMENT_ID', '')
requested = int(requested) if requested else None
config_names = [
    'LLM_MODEL', 'LLM_REVISION', 'LLM_TIMEOUT_SECONDS', 'LLM_MAX_OUTPUT_TOKENS',
    'CATALOG_SEGMENT_CHARS', 'CATALOG_MIN_SEGMENT_CHARS',
    'CATALOG_MAX_OUTPUT_TOKENS', 'CATALOG_VALIDATION_ATTEMPTS',
]
config = {key: os.getenv(key, '<unset>') for key in config_names}
config['effective_catalog_output_tokens'] = getattr(llm, 'CATALOG_MAX_OUTPUT_TOKENS', llm.LLM_MAX_OUTPUT_TOKENS)
config['catalog_version'] = catalog.CATALOG_VERSION
config['prompt_version'] = getattr(llm, 'CATALOG_PROMPT_VERSION', '<old runtime>')
emit('allowlisted configuration (no credentials)', config)

with get_conn() as conn:
    conn.execute('SET TRANSACTION READ ONLY')
    conn.execute("SET LOCAL statement_timeout = '15s'")
    emit('recent documents; compare the stuck ID with worker task args', conn.execute('''
        SELECT id, filename, status, catalog_status, error, catalog_error,
               pipeline_generation, updated_at
        FROM documents ORDER BY updated_at DESC, id DESC LIMIT 30
    ''').fetchall())
    emit('nonterminal documents, including old stuck records', conn.execute('''
        SELECT id, filename, status, catalog_status, error, catalog_error, updated_at
        FROM documents WHERE status IN ('queued', 'parsing', 'cataloging', 'extracting')
        ORDER BY updated_at ASC LIMIT 100
    ''').fetchall())
    emit('recent ingestion batches', conn.execute('''
        SELECT id, knowledge_base_id, status, updated_at
        FROM ingestion_batches ORDER BY id DESC LIMIT 5
    ''').fetchall())
    emit('recent maintenance jobs', conn.execute('''
        SELECT id, knowledge_base_id, document_id, mode, status, result, error, updated_at
        FROM maintenance_jobs ORDER BY updated_at DESC LIMIT 5
    ''').fetchall())
    emit('recent completed attempts (not an in-flight request list)', conn.execute('''
        SELECT id, document_id, chunk_id, stage, status, error, created_at
        FROM processing_attempts ORDER BY id DESC LIMIT 25
    ''').fetchall())
    emit('chunk status counts', conn.execute('''
        SELECT document_id, status, count(*) AS count
        FROM chunks
        WHERE document_id IN (SELECT id FROM documents ORDER BY updated_at DESC, id DESC LIMIT 30)
           OR document_id IN (SELECT id FROM documents WHERE status IN ('queued', 'parsing', 'cataloging', 'extracting'))
        GROUP BY document_id, status ORDER BY document_id DESC, status
    ''').fetchall())
    emit('database activity / blockers (current diagnostic session excluded)', conn.execute('''
        SELECT pid, state, wait_event_type, wait_event,
               now() - query_start AS query_age, pg_blocking_pids(pid) AS blockers,
               left(query, 220) AS query
        FROM pg_stat_activity
        WHERE datname = current_database() AND pid <> pg_backend_pid()
          AND (state IS DISTINCT FROM 'idle' OR wait_event_type = 'Lock')
        ORDER BY query_start LIMIT 20
    ''').fetchall())
    attempt = conn.execute('''
        SELECT * FROM processing_attempts
        WHERE stage = 'catalog' AND status = 'failed'
          AND position('Catalog mention is absent from its source segment' in coalesce(error, '')) > 0
          AND (%s::bigint IS NULL OR document_id = %s)
        ORDER BY id DESC LIMIT 1
    ''', (requested, requested)).fetchone()
    if not attempt:
        emit('matching failure', 'No matching audit row for this selection. Inspect worker traceback above/below; omit the ID to search all documents.')
    else:
        doc = conn.execute('SELECT * FROM documents WHERE id = %s', (attempt['document_id'],)).fetchone()
        chunk = conn.execute('SELECT * FROM chunks WHERE id = %s', (attempt['chunk_id'],)).fetchone()
        payload = attempt.get('output') or {}
        match = re.search(r'entity\[(\d+)\]', attempt.get('error') or '')
        index = int(match.group(1)) if match else None
        parsed = payload.get('parsed') or {}
        entities = parsed.get('entities') or []
        entity = entities[index] if index is not None and index < len(entities) else None
        emit('exact failed audit row (raw model output included)', attempt)
        emit('offending entity; index is zero-based', {'index': index, 'entity': entity})
        emit('document title and chunk metadata', {
            'document_id': doc['id'], 'title': doc.get('title'),
            'chunk_id': attempt['chunk_id'],
            'chunk_index': chunk.get('chunk_index') if chunk else None,
            'section_title': chunk.get('section_title') if chunk else None,
        })
        matched_segment = None
        if chunk and hasattr(catalog, 'split_catalog_segment') and hasattr(llm, 'CATALOG_PROMPT_VERSION'):
            # Reconstruct candidates only; never call the model or modify split caches.
            model_signature = catalog.digest([llm.LLM_BASE_URL, llm.LLM_MODEL, llm.LLM_REVISION])
            size = int(os.getenv('CATALOG_SEGMENT_CHARS', '12000'))
            minimum = int(os.getenv('CATALOG_MIN_SEGMENT_CHARS', '500'))
            if size > 0 and minimum > 0:
                stack = list(catalog.segments([chunk], max_chars=size))
                visited = 0
                while stack and visited < 20000:
                    segment = stack.pop()
                    visited += 1
                    heading = f"Document title: {doc['title'] or ''}\nSection: {segment['section_title'] or 'Unknown'}"
                    key = catalog.digest([catalog.CATALOG_VERSION, llm.CATALOG_PROMPT_VERSION,
                        TYPE_DEFINITIONS, model_signature, heading, segment['text'], doc['extraction_epoch']])
                    if key == attempt['version']:
                        matched_segment = segment
                        break
                    stack.extend(catalog.split_catalog_segment(segment, min_chars=minimum))
                if visited >= 20000 and matched_segment is None:
                    emit('segment reconstruction', 'Stopped at 20000 candidates; exact segment was not established.')
        if matched_segment:
            sources = [doc.get('title') or '', matched_segment.get('section_title') or '', matched_segment['text']]
            emit('exact segment (audit cache key matched)', {
                'start': matched_segment['start'], 'end': matched_segment['end'],
                'sources': sources,
            })
            if entity and hasattr(catalog, 'source_span'):
                values = [entity.get('mention'), entity.get('text')]
                emit('same-runtime span checks', [
                    {'value_repr': repr(value), 'matches': [
                        catalog.source_span(source, value) if value else None for source in sources
                    ]} for value in values
                ])
        else:
            emit('source context, NOT a verified exact segment', {
                'notice': 'Runtime/config/content may have changed since this attempt. The full chunk is context only, not proof that a child segment contained the entity.',
                'title': doc.get('title'),
                'section_title': chunk.get('section_title') if chunk else None,
                'chunk_text': chunk.get('text') if chunk else None,
            })
PY_DIAG
  printf '\n=== Celery active / reserved / scheduled (read-only inspection) ===\n'
  for kind in active reserved scheduled; do
    printf '\n--- %s ---\n' "$kind"
    docker compose exec -T worker_extract celery -A app.celery_app inspect "$kind" --timeout 10 || true
  done
  printf '\n=== Recent worker logs (last four hours, up to 1500 lines per service) ===\n'
  docker compose logs --since 4h --tail 1500 --timestamps worker_parse worker_extract || true
} 2>&1 | tee "$out"
printf '\nSaved locally: %s\nReview/redact credentials or private paper text before sharing.\n' "$out"

