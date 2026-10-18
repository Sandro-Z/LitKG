from psycopg.types.json import Jsonb
from contextlib import contextmanager
from dataclasses import replace
import os

from app.batch_status import sync_batches_for_document
from app.celery_app import celery_app
from app.db import get_conn
from app.graph_diagnostics import fetch_graph_diagnostics
from app.graph_store import lock_graph_projection, project_existing_claim, store_claim, upsert_entity
from app.llm import (LLM_MODEL, LLM_BASE_URL, LLM_REVISION, CATALOG_PROMPT_VERSION,
    CATALOG_MAX_OUTPUT_TOKENS, CLAIMS_MAX_OUTPUT_TOKENS, CLAIMS_PROMPT_VERSION,
    ModelOutputError, ModelOutputTruncatedError, extract_claims, extract_catalog)
from app.claims import ClaimValidationError, prepare_claims, combine_claims, retains_required_context
from app.entity_types import TYPE_DEFINITIONS
from app.catalog import (PROCESSING_VERSION, CATALOG_VERSION, CATALOG_CACHE_VERSION, EXTRACTION_VERSION, digest, segments,
    prepare_catalog, CatalogValidationError, split_catalog_segment, merge_catalog, relevant_entries, load_catalog, resolve_bundle, add_review, archive_claims, source_contains)
from app.normalization import entity_identity
from app.schemas import ClaimBundle, CatalogBundle, CatalogEntity
from app.normalization import extract_abbreviations
from app.parser import parse_pdf_to_chunks
from app.provenance import locate_evidence_pages
from app.recovery import heartbeat_lease

MODEL_SIGNATURE = digest([LLM_BASE_URL, LLM_MODEL, LLM_REVISION])


@contextmanager
def processing_lock(key):
    # Session lock covers LLM work without an open database transaction. Stage
    # caches commit independently, so a retry can resume after a failed segment.
    with get_conn() as conn:
        conn.autocommit = True
        acquired = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired", (key,)).fetchone()["acquired"]
        try:
            if acquired:
                with heartbeat_lease(conn, get_conn, key):
                    yield True
            else:
                yield False
        finally:
            if acquired:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


def cached_model(document_id, stage, key, model_call, schema, chunk_id=None, validate=None,
                 retry_call=None, max_attempts=1, retry_pending=False, excluded_candidates=None):
    def checked(result):
        result = result.model_copy(deep=True)
        if validate:
            validated = validate(result)
            if isinstance(validated, schema):
                return validated
        return result

    def record_failure(exc, parsed=None, raw_output=None):
        output = {'parsed': parsed, 'raw': getattr(exc, 'raw', raw_output)}
        with get_conn() as conn:
            conn.execute("""INSERT INTO processing_attempts (document_id, chunk_id, stage, version, status, output, error)
                VALUES (%s, %s, %s, %s, 'failed', %s, %s)""",
                (document_id, chunk_id, stage, key, Jsonb(output), f"{type(exc).__name__}: {exc}"))

    with get_conn() as conn:
        row = conn.execute("SELECT payload FROM processing_cache WHERE document_id = %s AND stage = %s AND cache_key = %s",
                           (document_id, stage, key)).fetchone()
    prefix = 'catalog' if stage == 'catalog' else 'claim'
    repairs_name, rejections_name = f'_{prefix}_repairs', f'_{prefix}_rejections'
    field, label = ('entities', 'entity') if stage == 'catalog' else ('claims', 'claim')
    resumed = None
    if row:
        try:
            saved = schema.model_validate(row['payload'])
            for name in (repairs_name, rejections_name):
                setattr(saved, name, row['payload'].get(name, []))
            if stage == 'claims':
                saved._repair_ids = row['payload'].get('_repair_ids', [])
                for item in saved._claim_rejections:
                    if 'claim' not in item:
                        item['claim'] = item.get('original')
            result = checked(saved)
            unresolved = any(r.get('outcome') != 'excluded' for r in getattr(result, rejections_name))
            if not row['payload'].get('_processing_gap') and not (unresolved and not getattr(result, field)) and not (retry_pending and unresolved):
                return result
            resumed = result
        except (ValueError, TypeError) as exc:
            record_failure(exc, row['payload'])
            with get_conn() as conn:
                conn.execute('DELETE FROM processing_cache WHERE document_id=%s AND stage=%s AND cache_key=%s',
                             (document_id, stage, key))
    last_error, original, raw_output = None, None, None
    attempt_limit = max(1, min(5, max_attempts))
    for attempt in range(attempt_limit):
        parsed, raw_output = None, None
        try:
            result = resumed if resumed is not None else model_call() if last_error is None else retry_call(last_error, original)
            parsed = result.model_dump(mode='json')
            raw_output = getattr(result, '_raw_output', None)
            result = checked(result)
            break
        except Exception as exc:
            record_failure(exc, parsed, raw_output)
            if isinstance(exc, ModelOutputTruncatedError) or retry_call is None or not isinstance(exc, (CatalogValidationError, ModelOutputError)) or attempt + 1 >= attempt_limit:
                raise
            last_error = exc
            original = getattr(exc, 'raw', None) or parsed
    # Correct only rejected candidates. Valid siblings are retained and never
    # sent through a second whole-bundle validation/correction pass.
    repairs = getattr(result, repairs_name)
    rejections = []
    for rejection_index, rejected in enumerate(getattr(result, rejections_name)):
        item = dict(rejected)
        if label not in item:
            item[label] = item.get('original')
        if stage == 'claims' and digest(item[label]) in (excluded_candidates or set()):
            item.update(outcome='excluded', reason='Confirmed invalid by manual review')
        if item.get('outcome') == 'excluded':
            rejections.append(item)
            continue
        item['outcome'] = 'unresolved'
        if retry_call is not None:
            repair_id = str(rejection_index)
            candidate = dict(item[label], repair_id=repair_id) if stage == 'claims' and isinstance(item[label], dict) else item[label]
            previous = {field: [candidate]}
            history = list(item.get('attempts', []))
            for correction_index in range(max(0, attempt_limit - 1)):
                correction = None
                try:
                    feedback = (f'repair_id={repair_id}: ' if stage == 'claims' else '') + item['error']
                    correction = checked(retry_call(CatalogValidationError(feedback), previous))
                    resolution = correction._resolution or {}
                    values = getattr(correction, field)
                    correction_errors = getattr(correction, rejections_name)
                    history.append({'parsed': correction.model_dump(mode='json'),
                                    'raw': correction._raw_output, 'resolution': resolution,
                                    'rejections': correction_errors})
                    if stage == 'claims' and resolution.get('repair_id') not in {None, repair_id}:
                        item['error'] = 'Correction resolution repair_id does not match the requested candidate'
                        continue
                    if resolution.get('outcome') == 'excluded' and not values and not correction_errors:
                        item.update(outcome='excluded', reason=resolution['reason'])
                        break
                    if len(values) == 1 and not correction_errors:
                        if stage == 'claims' and any(value is not None for value in correction._repair_ids) and correction._repair_ids != [repair_id]:
                            item['error'] = 'Correction repair_id does not match the requested candidate'
                            continue
                        if stage == 'claims' and isinstance(item[label], dict):
                            if not retains_required_context(item[label], values[0]):
                                item['error'] = 'Correction dropped required participants or condition fields; repair them or explicitly exclude the candidate'
                                continue
                        getattr(result, field).extend(values)
                        if stage == 'claims':
                            result._repair_ids.extend(correction._repair_ids or [None])
                        repairs.extend(getattr(correction, repairs_name))
                        repairs.append({f'{label}_index': item.get(f'{label}_index'), 'field': 'candidate',
                                        'original': item[label], 'replacement': values[0].model_dump(mode='json'),
                                        'outcome': 'repaired', 'attempts': history})
                        item = None
                        break
                    if resolution.get('outcome') == 'unresolved':
                        item['reason'] = resolution['reason']
                        break
                    item['error'] = correction_errors[0]['error'] if correction_errors else 'Correction must repair one candidate or explicitly confirm exclusion'
                except Exception as exc:
                    history.append({'error': f'{type(exc).__name__}: {exc}', 'raw': getattr(exc, 'raw', None)})
                    # Infrastructure/capacity failures stay visible in quality;
                    # good siblings are still usable. Never retry truncation.
                    if not isinstance(exc, (CatalogValidationError, ModelOutputError)) or isinstance(exc, ModelOutputTruncatedError):
                        break
                if correction is not None and len(getattr(correction, rejections_name)) == 1:
                    failed = getattr(correction, rejections_name)[0]
                    candidate = failed.get(label, failed.get('original'))
                    if stage == 'claims' and isinstance(candidate, dict):
                        candidate = dict(candidate, repair_id=repair_id)
                    previous = {field: [candidate]}
            if item is not None:
                item['attempts'] = history
        if item is not None:
            rejections.append(item)
    setattr(result, rejections_name, rejections)
    gap = not getattr(result, field) and any(r.get('outcome') != 'excluded' for r in rejections)
    payload = result.model_dump(mode='json')
    if stage == 'claims':
        payload['_repair_ids'] = result._repair_ids
    for name in (repairs_name, rejections_name):
        if getattr(result, name):
            payload[name] = getattr(result, name)
    if gap:
        payload['_processing_gap'] = True
    audit_status = 'failed' if gap else 'done'
    with get_conn() as conn:
        conn.execute("INSERT INTO processing_cache (document_id, stage, cache_key, payload) VALUES (%s, %s, %s, %s) ON CONFLICT (document_id,stage,cache_key) DO UPDATE SET payload=EXCLUDED.payload",
                     (document_id, stage, key, Jsonb(payload)))
        conn.execute(f"""INSERT INTO processing_attempts (document_id, chunk_id, stage, version, status, output, error)
            VALUES (%s, %s, %s, %s, '{audit_status}', %s, %s)""", (document_id, chunk_id, stage, key,
            Jsonb({'parsed': payload, 'original_parsed': parsed, 'raw': raw_output,
                   'repairs': repairs, 'rejections': rejections}), rejections[0]['error'] if gap else None))
    if gap:
        noun = 'catalog entries' if stage == 'catalog' else 'claims'
        message = rejections[0]['error'] + f'; no grounded {noun} remain'
        if stage == 'claims':
            raise ClaimValidationError(message, result)
        raise CatalogValidationError(message)

    return result


def claim_segment_bundles(document_id, segment, entries, version, retry_pending=False, excluded_candidates=None):
    """Bounded correction + durable split tree; one bad leaf cannot erase others."""
    minimum = int(os.environ.get('CLAIMS_MIN_SEGMENT_CHARS', '500'))
    attempts = max(1, min(5, int(os.environ.get('CLAIM_VALIDATION_ATTEMPTS', os.environ.get('CLAIMS_VALIDATION_ATTEMPTS', '2')))))
    key = digest([EXTRACTION_VERSION, CLAIMS_PROMPT_VERSION, MODEL_SIGNATURE, version,
                  segment['section_title'], segment['text'], minimum, CLAIMS_MAX_OUTPUT_TOKENS])
    split_key = digest(['claims-split-v1', key])
    with get_conn() as conn:
        split = conn.execute("SELECT payload FROM processing_cache WHERE document_id=%s AND stage=%s AND cache_key=%s",
                             (document_id, 'claims_split', split_key)).fetchone()
    directory = relevant_entries(entries, segment['text'])
    def invoke(**kwargs):
        return extract_claims(section_title=segment['section_title'] or 'Unknown',
                              chunk_text=segment['text'], directory=directory, **kwargs)
    def validate(bundle):
        return prepare_claims(bundle, entries, segment, version)
    try:
        if not split:
            bundle = cached_model(document_id, 'claims', key, invoke, ClaimBundle, segment['id'], validate=validate,
                retry_call=lambda error, previous:invoke(feedback=str(error), previous_output=previous),
                max_attempts=attempts, retry_pending=retry_pending, excluded_candidates=excluded_candidates)
            yield segment, bundle
            return
        children = split_catalog_segment(segment, minimum)
    except ModelOutputTruncatedError as exc:
        children = split_catalog_segment(segment, minimum)
        if not children:
            bundle = ClaimBundle(claims=[])
            bundle._claim_rejections = [{'error':f'{type(exc).__name__}: {exc}', 'kind':'capacity',
                                         'start':segment['start'], 'end':segment['end']}]
            bundle._processing_gaps = list(bundle._claim_rejections)
            yield segment, bundle
            return
        with get_conn() as conn:
            conn.execute("INSERT INTO processing_cache (document_id,stage,cache_key,payload) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (document_id,'claims_split',split_key,Jsonb({'strategy':'overlapping-halves-v1'})))
    except ClaimValidationError as exc:
        bundle = exc.bundle or ClaimBundle(claims=[])
        bundle._processing_gaps = [{'error': str(exc), 'kind': 'no_usable_claims',
                                    'start': segment['start'], 'end': segment['end']}]
        yield segment, bundle
        return
    except Exception as exc:
        bundle = ClaimBundle(claims=[])
        bundle._claim_rejections = [{'error':f'{type(exc).__name__}: {exc}', 'kind':'segment_failure',
                                     'start':segment['start'], 'end':segment['end']}]
        bundle._processing_gaps = list(bundle._claim_rejections)
        yield segment, bundle
        return
    if not children:
        raise ClaimValidationError('Invalid claims split cache')
    for child in children:
        yield from claim_segment_bundles(document_id, child, entries, version, retry_pending, excluded_candidates)


def extract_chunk_claims(document_id, chunk, entries, version, retry_pending=False):
    bundles = []
    excluded_candidates = set()
    if retry_pending:
        with get_conn() as conn:
            rows = conn.execute("""SELECT detail FROM review_items WHERE document_id=%s AND version=%s
                AND kind='claim_rejection' AND status='dismissed' AND detail->>'chunk_id'=%s""",
                (document_id, version, str(chunk['id']))).fetchall()
        excluded_candidates = {digest(row['detail'].get('claim', row['detail'].get('original'))) for row in rows}
    maximum = int(os.environ.get('CLAIMS_SEGMENT_CHARS', '3000'))
    for initial in segments([chunk], maximum):
        for segment, bundle in claim_segment_bundles(document_id, initial, entries, version, retry_pending, excluded_candidates):
            for item in [*bundle._claim_rejections, *bundle._claim_repairs]:
                item.update(start=segment['start'], end=segment['end'])
            bundle._claim_rejections = [item for item in bundle._claim_rejections if item.get('kind') not in {'capacity', 'segment_failure'}]
            bundles.append(bundle)
    return combine_claims(bundles)


def catalog_segment_bundles(document_id, title, segment, extraction_epoch, min_chars=None):
    """Persist successful leaves AND split plans, so retries resume the same tree."""
    if min_chars is None:
        min_chars = int(os.environ.get('CATALOG_MIN_SEGMENT_CHARS', '500'))
    if min_chars < 1:
        raise ValueError('CATALOG_MIN_SEGMENT_CHARS must be positive')
    heading = f"Document title: {title or ''}\nSection: {segment['section_title'] or 'Unknown'}"
    sources = [title or '', segment['section_title'] or '', segment['text']]
    validation_segment = {**segment, 'text':'\n'.join(sources), 'sources':sources}
    # Keep the successful-result key compatible with existing validated caches.
    key = digest([CATALOG_CACHE_VERSION, CATALOG_PROMPT_VERSION, TYPE_DEFINITIONS, MODEL_SIGNATURE,
                  heading, segment['text'], extraction_epoch])
    split_key = digest(['catalog-split-v1', key, min_chars])
    with get_conn() as conn:
        split = conn.execute("SELECT payload FROM processing_cache WHERE document_id=%s AND stage='catalog_split' AND cache_key=%s",
                             (document_id, split_key)).fetchone()
    if not split:
        try:
            bundle = cached_model(document_id, 'catalog', key,
                lambda:extract_catalog(heading, segment['text']), CatalogBundle, segment['id'],
                validate=lambda b:prepare_catalog(b, validation_segment, quarantine=True, allow_empty=True),
                retry_call=lambda error, previous:extract_catalog(heading, segment['text'], feedback=str(error), previous_output=previous),
                max_attempts=int(os.environ.get('CATALOG_VALIDATION_ATTEMPTS', '3')))
        except ModelOutputTruncatedError as exc:
            children = split_catalog_segment(segment, min_chars)
            if not children:
                raise ModelOutputTruncatedError(
                    f'Catalog output still truncated at {len(segment["text"])} input characters '
                    f'(CATALOG_MIN_SEGMENT_CHARS={min_chars}, CATALOG_MAX_OUTPUT_TOKENS={CATALOG_MAX_OUTPUT_TOKENS}). '
                    'Increase the catalog output budget within the model server limits; check its context/output limits and thinking configuration.', exc.raw) from exc
            with get_conn() as conn:
                conn.execute("INSERT INTO processing_cache (document_id,stage,cache_key,payload) VALUES (%s,'catalog_split',%s,%s) ON CONFLICT DO NOTHING",
                             (document_id, split_key, Jsonb({'strategy':'overlapping-halves-v1'})))
        else:
            yield segment, bundle
            return
    else:
        children = split_catalog_segment(segment, min_chars)
    if not children:
        raise ValueError('Invalid catalog split cache: segment cannot be subdivided')
    for child in children:
        yield from catalog_segment_bundles(document_id, title, child, extraction_epoch, min_chars)


def update_document_status(
    document_id: int,
    status: str,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET status = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, error, document_id),
            )
    sync_batches_for_document(document_id)


def update_chunk_status(
    chunk_id: int,
    status: str,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE chunks
                SET status = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, error, chunk_id),
            )


def refresh_document_status(document_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)::INT AS total,
                    COUNT(*) FILTER (WHERE status = 'done')::INT AS done,
                    COUNT(*) FILTER (WHERE status = 'failed')::INT AS failed,
                    COUNT(*) FILTER (WHERE status = 'partial')::INT AS partial,
                    COUNT(*) FILTER (
                        WHERE status IN ('queued', 'extracting')
                    )::INT AS active,
                    STRING_AGG(error, '; ' ORDER BY id)
                        FILTER (WHERE status IN ('failed','partial') AND error IS NOT NULL)
                        AS errors
                FROM chunks
                WHERE document_id = %s
                """,
                (document_id,),
            )
            stats = cur.fetchone()

            if stats["total"] == 0:
                status = "failed"
                error = "No chunks were generated"
            elif stats["active"] > 0:
                status = "extracting"
                error = None
            elif stats["failed"] == 0 and stats["partial"] == 0:
                status = "done"
                error = None
            elif stats["done"] == 0 and stats["partial"] == 0:
                status = "failed"
                error = stats["errors"]
            else:
                status = "partial"
                error = stats["errors"]

            cur.execute(
                """
                UPDATE documents
                SET status = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, error, document_id),
            )

    sync_batches_for_document(document_id)
    return status


@celery_app.task(name="parse_document", queue="parse")
def parse_document(document_id: int, publish: bool = True):
    with processing_lock(f"litkg-document:{document_id}") as acquired:
        if not acquired:
            return {"document_id": document_id, "status": "busy"}
        try:
            update_document_status(document_id, "parsing")
            with get_conn() as conn:
                document = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
            if not document:
                return {"document_id": document_id, "status": "superseded"}
            metadata, parsed_chunks = parse_pdf_to_chunks(document["source_path"])
            if not parsed_chunks or not any(c["text"].strip() for c in parsed_chunks):
                raise ValueError("Parser produced no text; check PDF text layer or OCR")
            abbreviations = extract_abbreviations("\n".join(c["text"] for c in parsed_chunks))
            with get_conn() as conn:
                with conn.cursor() as cur:
                    lock_graph_projection(cur)
                    archive_claims(cur, document_id)
                    cur.execute("""UPDATE documents SET title = COALESCE(%s, title), doi = COALESCE(%s, doi),
                        abbreviation_map = %s, status = 'cataloging', catalog_status = 'pending', catalog_error = NULL,
                        pipeline_generation = pipeline_generation + 1, error = NULL, updated_at = now() WHERE id = %s""",
                        (metadata.get("title"), metadata.get("doi"), Jsonb(abbreviations), document_id))
                    for index, chunk in enumerate(parsed_chunks):
                        # Keep unchanged claims until a successful replacement. Original
                        # source snapshots were archived before any text was changed.
                        cur.execute("""INSERT INTO chunks (document_id, chunk_index, section_title, page_start, page_end,
                            page_spans, text, token_estimate, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued')
                            ON CONFLICT (document_id, chunk_index) DO UPDATE SET section_title = EXCLUDED.section_title,
                            page_start = EXCLUDED.page_start, page_end = EXCLUDED.page_end, page_spans = EXCLUDED.page_spans,
                            text = EXCLUDED.text, token_estimate = EXCLUDED.token_estimate, status = 'queued',
                            error = NULL, updated_at = now()""",
                            (document_id, index, chunk.get("section_title"), chunk.get("page_start"), chunk.get("page_end"),
                             Jsonb(chunk.get("page_spans", [])), chunk["text"], chunk.get("token_estimate")))
                    cur.execute("DELETE FROM chunks WHERE document_id = %s AND chunk_index >= %s", (document_id, len(parsed_chunks)))
            if publish:
                build_document_catalog.apply_async(args=[document_id], queue="extract")
            return {"document_id": document_id, "chunk_count": len(parsed_chunks), "status": "cataloging"}
        except Exception as exc:
            with get_conn() as conn:
                if not conn.execute("SELECT id FROM documents WHERE id=%s", (document_id,)).fetchone():
                    return {"document_id": document_id, "status": "superseded"}
            update_document_status(document_id, "failed", f"Parse failed: {type(exc).__name__}: {exc}")
            raise


@celery_app.task(name="build_document_catalog", queue="extract")
def build_document_catalog(document_id: int, force: bool = False, publish: bool = True):
    with processing_lock(f"litkg-document:{document_id}") as acquired:
        if not acquired:
            return {"document_id": document_id, "status": "busy"}
        version = None
        try:
            with get_conn() as conn:
                document = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
                if not document:
                    return {"document_id": document_id, "status": "superseded"}
                chunks = conn.execute("SELECT * FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,)).fetchall()
                if force:
                    document = conn.execute("""UPDATE documents SET pipeline_generation=pipeline_generation+1,
                        extraction_epoch=extraction_epoch+1 WHERE id=%s RETURNING *""", (document_id,)).fetchone()
            if not chunks:
                raise ValueError("No saved chunks; PDF must be parsed first")
            abbreviations = extract_abbreviations('\n'.join([document['title'] or '', *[f"{c['section_title'] or ''}\n{c['text']}" for c in chunks]]))
            content_hash = digest([document['title'], [(c["chunk_index"], c['section_title'], c["text"], c["page_spans"]) for c in chunks]])
            generation = document["pipeline_generation"]
            version = digest([content_hash, CATALOG_VERSION, CATALOG_PROMPT_VERSION, TYPE_DEFINITIONS, MODEL_SIGNATURE, document['extraction_epoch']])
            with get_conn() as conn:
                conn.execute("""UPDATE documents SET status = 'cataloging', catalog_status = 'processing',
                    catalog_version = %s, abbreviation_map = %s, catalog_error = NULL, error = NULL WHERE id = %s""",
                    (version, Jsonb(abbreviations), document_id))
                conn.execute("""INSERT INTO document_catalogs (document_id, version, status, content_hash, processing_version)
                    VALUES (%s,%s,'processing',%s,%s) ON CONFLICT (document_id,version) DO UPDATE SET status = 'processing', error = NULL""",
                    (document_id, version, content_hash, CATALOG_VERSION))
                if document['catalog_version'] != version or force:
                    conn.execute("UPDATE chunks SET status='queued',error=NULL WHERE document_id=%s", (document_id,))
            records = []
            rejected_count = 0
            for initial_segment in segments(chunks, max_chars=int(os.environ.get("CATALOG_SEGMENT_CHARS", "12000"))):
                for segment, bundle in catalog_segment_bundles(document_id, document['title'], initial_segment, document['extraction_epoch']):
                    rejected_count += len(bundle._catalog_rejections)
                    if bundle._catalog_repairs or bundle._catalog_rejections:
                        with get_conn() as conn:
                            for repair in bundle._catalog_repairs:
                                add_review(conn.cursor(), document_id, version, 'catalog_repair',
                                    {'chunk_id': segment['id'], 'start': segment['start'], **repair}, status='resolved')
                            for rejection in bundle._catalog_rejections:
                                add_review(conn.cursor(), document_id, version, 'catalog_rejection',
                                    {'chunk_id': segment['id'], 'start': segment['start'], 'end': segment['end'],
                                     'sources': [document['title'] or '', segment['section_title'] or '', segment['text']],
                                     **rejection}, status='dismissed' if rejection.get('outcome') == 'excluded' else 'pending')
                    for entity in bundle.entities:
                        page, page_end = locate_evidence_pages(entity.evidence, next(c for c in chunks if c["id"] == segment["id"]))
                        source_kind = 'text'
                        if not source_contains(segment['text'], entity.evidence):
                            source_kind = 'document_title' if source_contains(document['title'] or '',entity.evidence) else 'section_title'
                            page, page_end = None, None
                        records.append((entity, {"chunk_id": segment["id"], "start": segment["start"], "end": segment["end"],
                                                "source_kind":source_kind,"page": page,"page_end": page_end,"evidence": entity.evidence}))
            entries = merge_catalog(records, abbreviations, document_id)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    lock_graph_projection(cur, True)
                    for entry in entries:
                        identity = entity_identity(entry["text"], entry["type"], entry["normalized_id"], abbreviations, document_id, entry.get('pending_merge', False))
                        identity = replace(identity,aliases=tuple(entry['aliases']))
                        entity_id = upsert_entity(cur, identity)
                        for identifier in entry.get('identifiers',[]):
                            other = entity_identity(entry['text'],entry['type'],identifier,abbreviations,document_id)
                            upsert_entity(cur,replace(other,aliases=tuple(entry['aliases'])))
                        cur.execute("""INSERT INTO document_entities (document_id, catalog_version, local_id, entity_id, entry)
                            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (document_id,catalog_version,local_id)
                            DO UPDATE SET entry = EXCLUDED.entry, entity_id = EXCLUDED.entity_id""",
                            (document_id, version, entry["local_id"], entity_id, Jsonb(entry)))
                        if entry["ambiguity"] or entry["type"] == "Other":
                            add_review(cur, document_id, version, "catalog_entity", entry)
                    cur.execute("UPDATE document_catalogs SET status = 'done', error = NULL WHERE document_id = %s AND version = %s", (document_id, version))
                    cur.execute("""UPDATE documents SET catalog_status = 'done', catalog_error = NULL,
                        status = 'extracting', error = NULL, updated_at = now() WHERE id = %s""", (document_id,))
            # The committed complete catalog is visible before the FIRST relation task.
            if publish:
                for chunk in chunks:
                    extract_chunk.apply_async(args=[chunk["id"], version, generation], queue="extract")
            return {"document_id": document_id, "catalog_version": version, "entity_count": len(entries),
                    "chunk_count": len(chunks), "rejected_candidates": rejected_count}
        except Exception as exc:
            with get_conn() as conn:
                if not conn.execute("SELECT id FROM documents WHERE id=%s", (document_id,)).fetchone():
                    return {"document_id": document_id, "status": "superseded"}
                conn.execute("UPDATE documents SET catalog_status = 'failed', catalog_error = %s WHERE id = %s", (f"{type(exc).__name__}: {exc}", document_id))
                if version:
                    conn.execute("UPDATE document_catalogs SET status = 'failed', error = %s WHERE document_id = %s AND version = %s", (str(exc), document_id, version))
            update_document_status(document_id, "failed", f"Catalog failed: {type(exc).__name__}: {exc}")
            raise


@celery_app.task(name="extract_chunk", queue="extract")
def extract_chunk(chunk_id: int, catalog_version: str | None = None, generation: int | None = None, refresh: bool = True, retry_pending: bool = False):
    document_id = None
    with processing_lock(f"litkg-chunk:{chunk_id}") as acquired:
        if not acquired:
            return {"chunk_id": chunk_id, "status": "busy"}
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT ch.*, d.abbreviation_map, d.catalog_version, d.catalog_status, d.pipeline_generation
                        FROM chunks ch JOIN documents d ON d.id = ch.document_id WHERE ch.id = %s""", (chunk_id,))
                    chunk = cur.fetchone()
                    if not chunk:
                        return {"chunk_id": chunk_id, "status": "superseded"}
                    document_id = chunk["document_id"]
                    version = chunk["catalog_version"]
                    if (catalog_version is not None and catalog_version != version) or (generation is not None and generation != chunk["pipeline_generation"]):
                        return {"chunk_id": chunk_id, "status": "superseded"}
                    if chunk["catalog_status"] != "done":
                        raise ValueError("A complete document catalog is required before relation extraction")
                    entries = load_catalog(cur, document_id, version)
            update_chunk_status(chunk_id, "extracting")
            key = digest([EXTRACTION_VERSION, MODEL_SIGNATURE, version, chunk['section_title'], chunk["text"]])
            bundle = extract_chunk_claims(document_id, chunk, entries, version, retry_pending=retry_pending)
            rejected, gaps = bundle._claim_rejections, bundle._processing_gaps
            if gaps and not bundle.claims:
                with get_conn() as conn:
                    for kind, items in [('claim_rejection', rejected), ('processing_gap', gaps)]:
                        for item in items:
                            add_review(conn.cursor(), document_id, version, kind, {'chunk_id': chunk_id, **item},
                                status='dismissed' if item.get('outcome') == 'excluded' else 'pending')
                raise ClaimValidationError(f'{len(gaps)} unresolved segment errors: ' + '; '.join(item['error'] for item in gaps))
            raw_claims = [c.model_dump(mode='json') for c in bundle.claims]
            omissions = [(entity, {'chunk_id': chunk_id, 'mention': entity.mention or entity.text, 'role': role})
                for claim in bundle.claims
                for role, entity in [('subject', claim.subject), ('object', claim.object), *[(p.role, p.entity) for p in claim.participants]]
                if entity.pending_merge]
            with get_conn() as conn:
                with conn.cursor() as cur:
                    lock_graph_projection(cur, True)
                    cur.execute("SELECT catalog_version, catalog_status, pipeline_generation FROM documents WHERE id = %s FOR UPDATE", (document_id,))
                    current = cur.fetchone()
                    if current["catalog_version"] != version or current["catalog_status"] != "done" or current["pipeline_generation"] != chunk["pipeline_generation"]:
                        return {"chunk_id": chunk_id, "status": "superseded"}
                    archive_claims(cur, document_id, chunk_id)
                    if not gaps:
                        cur.execute("""UPDATE review_items SET status='resolved',updated_at=now()
                            WHERE document_id=%s AND version=%s AND status='pending' AND detail->>'chunk_id'=%s
                              AND (kind='processing_gap' OR (kind='claim_rejection' AND detail->>'kind' IN ('capacity','segment_failure')))""",
                            (document_id, version, str(chunk_id)))
                    for kind, items in [('claim_repair', bundle._claim_repairs), ('claim_rejection', rejected), ('processing_gap', gaps)]:
                        for item in items:
                            repaired = kind == 'claim_repair' and item.get('field') == 'candidate'
                            excluded = kind == 'claim_rejection' and item.get('outcome') == 'excluded'
                            if repaired or excluded:
                                original = item['original'] if repaired else item.get('claim', item.get('original'))
                                cur.execute("""UPDATE review_items SET status=%s,updated_at=now()
                                    WHERE document_id=%s AND version=%s AND kind='claim_rejection' AND status='pending'
                                      AND detail->>'chunk_id'=%s AND COALESCE(detail->'claim',detail->'original')=%s""",
                                    ('resolved' if repaired else 'dismissed', document_id, version, str(chunk_id), Jsonb(original)))
                            add_review(cur, document_id, version, kind, {'chunk_id': chunk_id, **item},
                                status='dismissed' if item.get('outcome') == 'excluded' else 'resolved' if kind == 'claim_repair' else 'pending')
                    kept = []
                    for claim, raw_claim in zip(bundle.claims, raw_claims):
                        for evidence in [claim.evidence, *claim.additional_evidence]:
                            evidence.page, evidence.page_end = locate_evidence_pages(evidence.sentence, chunk)
                            evidence.section = chunk["section_title"]
                        raw_claim['evidence'] = claim.evidence.model_dump(mode='json')
                        raw_claim['additional_evidence'] = [e.model_dump(mode='json') for e in claim.additional_evidence]
                        claim_id, _ = store_claim(cur, document_id=document_id, chunk_id=chunk_id, claim=claim,
                                                 extraction_model=LLM_MODEL, abbreviations=chunk["abbreviation_map"], original_claim_json=raw_claim)
                        kept.append(claim_id)
                    if not gaps:
                        cur.execute("DELETE FROM claims WHERE chunk_id = %s AND NOT (id = ANY(%s::BIGINT[]))", (chunk_id, kept))
                    # Missing directory items are appended to this immutable source/
                    # processing version; existing local IDs never change.
                    for entity, location in omissions:
                        raw = CatalogEntity(**entity.model_dump(), aliases=[], evidence=entity.mention or entity.text)
                        entry = merge_catalog([(raw, location)], chunk["abbreviation_map"], document_id)[0]
                        entity_id = upsert_entity(cur, entity_identity(entry["text"], entry["type"], entry["normalized_id"], chunk["abbreviation_map"], document_id, entry.get('pending_merge', False)))
                        cur.execute("""INSERT INTO document_entities (document_id,catalog_version,local_id,entity_id,entry)
                            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (document_id,catalog_version,local_id) DO UPDATE
                            SET entry=jsonb_set(jsonb_set(document_entities.entry,'{aliases}',
                              (SELECT jsonb_agg(DISTINCT value) FROM jsonb_array_elements((document_entities.entry->'aliases') || (EXCLUDED.entry->'aliases')))),
                              '{locations}',(SELECT jsonb_agg(DISTINCT value) FROM jsonb_array_elements((document_entities.entry->'locations') || (EXCLUDED.entry->'locations'))))""",
                            (document_id,version,entry["local_id"],entity_id,Jsonb(entry)))
                        add_review(cur, document_id, version, "catalog_omission", {"entry": entry, "location": location})
                    chunk_status = 'partial' if gaps else 'done'
                    error = (f'{len(gaps)} unresolved segment errors: ' + '; '.join(item['error'] for item in gaps)) if gaps else None
                    cur.execute("UPDATE chunks SET status=%s, extraction_version=%s, error=%s, updated_at=now() WHERE id=%s",
                                (chunk_status, key, error, chunk_id))
            status = refresh_document_status(document_id)
            if refresh and status in {"done", "partial"}:
                refresh_derived.apply_async(args=[document_id], queue="extract")
            return {"chunk_id": chunk_id, "document_id": document_id, "claim_count": len(bundle.claims),
                    'status':chunk_status, 'rejected_count':len(rejected), 'processing_gap_count':len(gaps)}
        except Exception as exc:
            if document_id is not None:
                with get_conn() as conn:
                    current = conn.execute('SELECT catalog_version,pipeline_generation FROM documents WHERE id=%s', (document_id,)).fetchone()
                if not current or (current['catalog_version'] != chunk['catalog_version'] or current['pipeline_generation'] != chunk['pipeline_generation']):
                    return {'chunk_id':chunk_id,'status':'superseded'}
            update_chunk_status(chunk_id, "failed", f"{type(exc).__name__}: {exc}")
            if document_id is not None:
                refresh_document_status(document_id)
            raise


def _rebuild_knowledge_graph(knowledge_base_id: int):
    last_claim_id = 0
    projected_count = 0
    failed_count = 0
    failure_samples = []
    abbreviations_by_document = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            before = fetch_graph_diagnostics(cur, knowledge_base_id)
            cur.execute(
                """
                SELECT d.id, d.status,
                    EXISTS (SELECT 1 FROM chunks ch WHERE ch.document_id = d.id AND ch.status = 'extracting') AS active_chunks
                FROM documents d
                JOIN knowledge_base_documents member ON member.document_id = d.id
                WHERE member.knowledge_base_id = %s
                ORDER BY d.id
                """,
                (knowledge_base_id,),
            )
            documents = cur.fetchall()
    active_statuses = {"queued", "parsing", "cataloging", "extracting", "chunked"}
    document_ids = [document["id"] for document in documents if document["status"] not in active_statuses and not document["active_chunks"]]
    for document_id in document_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                archive_claims(cur, document_id)
                cur.execute("SELECT text FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,))
                abbreviations = extract_abbreviations("\n".join(row["text"] for row in cur.fetchall()))
                abbreviations_by_document[document_id] = abbreviations
                cur.execute("UPDATE documents SET abbreviation_map = %s WHERE id = %s", (Jsonb(abbreviations), document_id))

    while document_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.document_id, c.claim_json
                    FROM claims c
                    JOIN knowledge_base_documents member
                      ON member.document_id = c.document_id
                    WHERE member.knowledge_base_id = %s
                      AND c.id > %s
                      AND c.document_id = ANY(%s::BIGINT[])
                    ORDER BY c.id
                    LIMIT 500
                    """,
                    (knowledge_base_id, last_claim_id, document_ids),
                )
                rows = cur.fetchall()

        if not rows:
            break

        with get_conn() as conn:
            for row in rows:
                last_claim_id = row["id"]
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            project_existing_claim(
                                cur,
                                claim_id=row["id"],
                                claim_json=row["claim_json"],
                                abbreviations=abbreviations_by_document[row["document_id"]],
                                document_id=row["document_id"],
                            )
                    projected_count += 1
                except Exception as exc:
                    failed_count += 1
                    if len(failure_samples) < 20:
                        failure_samples.append(
                            {
                                "claim_id": row["id"],
                                "error": str(exc),
                            }
                        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            after = fetch_graph_diagnostics(cur, knowledge_base_id)
            from app.retrieval import rebuild_derived
            derived = rebuild_derived(cur, knowledge_base_id, document_ids)
    return {
        "knowledge_base_id": knowledge_base_id,
        "projected_count": projected_count,
        "failed_count": failed_count,
        "failure_samples": failure_samples,
        "skipped_active_document_count": len(documents) - len(document_ids),
        "abbreviation_count": sum(bool(value) for values in abbreviations_by_document.values() for value in values.values()),
        "ambiguous_abbreviation_count": sum(value is None for values in abbreviations_by_document.values() for value in values.values()),
        "before": before,
        "after": after,
        "derived": derived,
    }


def _update_rebuild_job(job_id, knowledge_base_id, status, result=None, error=None):
    if job_id is None:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE graph_rebuild_jobs
                SET status = %s, result = %s, error = %s, updated_at = now()
                WHERE id = %s::UUID AND knowledge_base_id = %s
                """,
                (status, Jsonb(result) if result is not None else None, error, job_id, knowledge_base_id),
            )


@celery_app.task(name="rebuild_knowledge_graph", queue="extract")
def rebuild_knowledge_graph(knowledge_base_id: int, job_id: str | None = None):
    _update_rebuild_job(job_id, knowledge_base_id, "processing")
    try:
        result = _rebuild_knowledge_graph(knowledge_base_id)
        status = "partial" if result["failed_count"] or result["skipped_active_document_count"] else "done"
        _update_rebuild_job(job_id, knowledge_base_id, status, result=result)
        return result
    except Exception as exc:
        _update_rebuild_job(job_id, knowledge_base_id, "failed", error=f"Graph rebuild failed: {type(exc).__name__}")
        raise


@celery_app.task(name="refresh_derived", queue="extract")
def refresh_derived(document_id: int):
    from app.retrieval import rebuild_derived
    try:
        with get_conn() as conn:
            if not conn.execute("SELECT id FROM documents WHERE id=%s FOR KEY SHARE", (document_id,)).fetchone():
                return {"document_id": document_id, "status": "superseded"}
            conn.execute("INSERT INTO derived_dirty_documents (document_id) VALUES (%s) ON CONFLICT DO NOTHING", (document_id,))
            dirty = conn.execute('SELECT document_id,revision FROM derived_dirty_documents ORDER BY (document_id=%s) DESC,document_id LIMIT 500', (document_id,)).fetchall()
            affected_ids = [r['document_id'] for r in dirty]
            rows = conn.execute("SELECT DISTINCT knowledge_base_id FROM knowledge_base_documents WHERE document_id=ANY(%s::BIGINT[]) ORDER BY knowledge_base_id", (affected_ids,)).fetchall()
        results = []
        for row in rows:
            kb = row["knowledge_base_id"]
            with get_conn() as conn:
                # A single KB commit prevents concurrent partial refreshes.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"litkg-derived:{kb}",))
                results.append(rebuild_derived(conn.cursor(), kb, affected_ids))
        with get_conn() as conn:
            if not conn.execute("SELECT id FROM documents WHERE id=%s FOR KEY SHARE", (document_id,)).fetchone():
                return {"document_id": document_id, "status": "superseded"}
            for row in dirty:
                conn.execute('DELETE FROM derived_dirty_documents WHERE document_id=%s AND revision=%s', (row['document_id'],row['revision']))
            conn.execute("""INSERT INTO processing_attempts (document_id,stage,version,status,output)
                VALUES (%s,'derived',%s,'done',%s)""", (document_id,PROCESSING_VERSION,Jsonb(results)))
            pending = conn.execute('SELECT document_id FROM derived_dirty_documents ORDER BY document_id LIMIT 1').fetchone()
        if pending:
            refresh_derived.apply_async(args=[pending['document_id']],queue='extract')
        return results
    except Exception as exc:
        with get_conn() as conn:
            if not conn.execute("SELECT id FROM documents WHERE id=%s FOR KEY SHARE", (document_id,)).fetchone():
                return {"document_id": document_id, "status": "superseded"}
            conn.execute("""INSERT INTO processing_attempts (document_id,stage,version,status,error)
                VALUES (%s,'derived',%s,'failed',%s)""", (document_id,PROCESSING_VERSION,f"{type(exc).__name__}: {exc}"))
        raise


@celery_app.task(name="maintain_knowledge_base", queue="extract")
def maintain_knowledge_base(job_id: str):
    from app.retrieval import rebuild_derived
    with processing_lock(f"litkg-maintenance:{job_id}") as acquired:
        if not acquired:
            return {"status": "busy"}
        with get_conn() as conn:
            job = conn.execute("SELECT * FROM maintenance_jobs WHERE id=%s::UUID", (job_id,)).fetchone()
            if not job or job["status"] == "done":
                return {"status": "done"}
            conn.execute("UPDATE maintenance_jobs SET status='processing',error=NULL,updated_at=now() WHERE id=%s::UUID", (job_id,))
            rows = conn.execute("""SELECT d.* FROM documents d JOIN knowledge_base_documents m ON m.document_id=d.id
                WHERE m.knowledge_base_id=%s AND (%s::BIGINT IS NULL OR d.id=%s) ORDER BY d.id""",
                (job["knowledge_base_id"],job["document_id"],job["document_id"])).fetchall()
        results = []
        try:
            for document in rows:
                doc_id = document["id"]
                if job['mode'] != 'retry' and document["status"] in {"parsing", "cataloging", "extracting", "queued"}:
                    results.append({"document_id":doc_id,"status":"skipped_active"})
                    continue
                try:
                    document_status = 'done'
                    if job["mode"] == "derived":
                        result = refresh_derived.run(doc_id)
                    elif job["mode"] == "reproject":
                        with processing_lock(f"litkg-document:{doc_id}") as locked:
                            if not locked:
                                raise ValueError("Document is busy")
                            with get_conn() as conn:
                                cur = conn.cursor()
                                lock_graph_projection(cur, True)
                                archive_claims(cur, doc_id)
                                chunks = conn.execute("SELECT text FROM chunks WHERE document_id=%s ORDER BY chunk_index", (doc_id,)).fetchall()
                                abbreviations = extract_abbreviations("\n".join(c["text"] for c in chunks))
                                claims = conn.execute("SELECT id,claim_json FROM claims WHERE document_id=%s ORDER BY id", (doc_id,)).fetchall()
                                for claim in claims:
                                    project_existing_claim(cur, claim["id"], claim["claim_json"], abbreviations, doc_id)
                                conn.execute("UPDATE documents SET abbreviation_map=%s WHERE id=%s", (Jsonb(abbreviations), doc_id))
                            result = {"projected_count":len(claims),"llm_called":False,"catalog_complete":document["catalog_status"] == "done"}
                        refresh_derived.run(doc_id)
                    else:
                        force = job["mode"] == "reextract"
                        with get_conn() as conn:
                            saved = conn.execute('SELECT count(*) AS n FROM chunks WHERE document_id=%s', (doc_id,)).fetchone()['n']
                        if not saved:
                            parsed = parse_document.run(doc_id, publish=False)
                            if parsed.get('status') == 'busy':
                                raise ValueError('Document is busy')
                        if not saved or force or document["catalog_status"] != "done":
                            result = build_document_catalog.run(doc_id, force=force, publish=False)
                            if result.get("status") == "busy":
                                raise ValueError("Document is busy")
                        with get_conn() as conn:
                            current = conn.execute("SELECT catalog_version,pipeline_generation FROM documents WHERE id=%s", (doc_id,)).fetchone()
                            chunks = conn.execute("""SELECT id FROM chunks WHERE document_id=%s AND (%s OR status <> 'done'
                                OR EXISTS (SELECT 1 FROM review_items r WHERE r.document_id=chunks.document_id
                                    AND r.version=%s AND r.kind='claim_rejection' AND r.status='pending'
                                    AND r.detail->>'chunk_id'=chunks.id::TEXT)) ORDER BY chunk_index""",
                                (doc_id,force,current['catalog_version'])).fetchall()
                        chunk_results = []
                        superseded = False
                        for chunk in chunks:
                            try:
                                chunk_result = extract_chunk.run(chunk["id"], current["catalog_version"], current["pipeline_generation"], refresh=False, retry_pending=job['mode'] == 'retry')
                                chunk_result.setdefault('status', 'done')
                            except Exception as exc:
                                # extract_chunk has persisted this failure. Keep
                                # visiting the remaining chunks: no broker tasks
                                # were published for this synchronous job.
                                chunk_result = {'status':'failed', 'error':f'{type(exc).__name__}: {exc}'}
                            chunk_results.append({'chunk_id':chunk['id'], **chunk_result})
                            if chunk_result.get('status') == 'superseded':
                                superseded = True
                                break
                        if superseded:
                            # A new generation owns the document now. Do not
                            # finalize or relabel its chunks from this old job.
                            document_status = 'superseded'
                        else:
                            final_status = refresh_document_status(doc_id)
                            refresh_derived.run(doc_id)
                            document_status = final_status if final_status in {'done','partial','failed'} else 'partial'
                        result = {"processed_chunks":len(chunk_results), "llm_required":True,
                                  "chunks":chunk_results}
                    results.append({"document_id":doc_id,"status":document_status,"result":result})
                except Exception as exc:
                    results.append({"document_id":doc_id,"status":"failed","error":f"{type(exc).__name__}: {exc}"})
            status = "done" if all(r["status"] == "done" for r in results) else "partial"
            with get_conn() as conn:
                conn.execute("UPDATE maintenance_jobs SET status=%s,result=%s,updated_at=now() WHERE id=%s::UUID", (status,Jsonb(results),job_id))
            return {"status":status,"documents":results}
        except Exception as exc:
            with get_conn() as conn:
                conn.execute("UPDATE maintenance_jobs SET status='failed',error=%s,updated_at=now() WHERE id=%s::UUID", (f"{type(exc).__name__}: {exc}",job_id))
            raise
