"""Shared claims pagination and complete, snapshot-consistent exports."""
import csv
import io
import json

from app.claim_semantics import result_sql, result_view, display_negated
from app.normalization import normalize_entity_type, normalize_predicate
from app.provenance import add_claim_location


def fetch_claim_page(cur, *, knowledge_base_id=None, document_id=None, limit=100,
                     offset=0, after_id=None, predicate=None, entity_type=None, result_status=None):
    filters, params = [], []
    if knowledge_base_id is not None:
        filters.append('EXISTS (SELECT 1 FROM knowledge_base_documents member WHERE member.document_id=claim.document_id AND member.knowledge_base_id=%s)')
        params.append(knowledge_base_id)
    elif document_id is not None:
        filters.append('claim.document_id=%s')
        params.append(document_id)
    else:
        raise ValueError('A claims scope is required')
    if predicate:
        filters.append('claim.predicate=%s')
        params.append(normalize_predicate(predicate))
    if entity_type:
        filters.append('(claim.subject_type=%s OR claim.object_type=%s)')
        params.extend([normalize_entity_type(entity_type)] * 2)
    if result_status:
        filters.append(f'({result_sql("claim")})=%s')
        params.append(result_status)
    where = ' AND '.join(filters)
    cur.execute(f'SELECT COUNT(*)::INT AS total FROM claims claim WHERE {where}', tuple(params))
    total = cur.fetchone().get('total', 0)
    if after_id is not None:
        where += ' AND claim.id>%s'
        params.append(after_id)
    cur.execute(f"""SELECT claim.*, document.filename, document.title, document.doi,
        document.pipeline_generation AS document_pipeline_generation, chunk.extraction_version AS chunk_extraction_version,
        chunk.section_title, chunk.page_start AS chunk_page_start, chunk.page_end AS chunk_page_end,
        (SELECT f.id FROM facts f WHERE f.claim_id=claim.id) AS fact_id,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', r.id, 'kind', r.kind, 'status', r.status, 'note', r.note) ORDER BY r.id)
          FROM review_items r WHERE r.document_id=claim.document_id
          AND (r.detail->>'claim_id'=claim.id::TEXT OR r.detail->>'chunk_id'=claim.chunk_id::TEXT)), '[]'::jsonb) AS reviews
        FROM claims claim JOIN documents document ON document.id=claim.document_id
        LEFT JOIN chunks chunk ON chunk.id=claim.chunk_id
        WHERE {where} ORDER BY claim.id LIMIT %s OFFSET %s""", (*params, limit, offset))
    rows = []
    for row in cur.fetchall():
        item = add_claim_location(row)
        raw = item.get('claim_json') or {}
        item.update(qualifiers=raw.get('qualifiers', {}), participants=raw.get('participants', []),
                    process={role: (raw.get(role) or {}).get('process') for role in ('subject', 'object')},
                    additional_evidence=raw.get('additional_evidence', []), source_kind=raw.get('source_kind', 'unknown'),
                    result_status=result_view(item), semantic_status=raw.get('semantic_status', 'unknown'))
        item['review_status'] = 'pending' if item['semantic_status'] == 'pending' or any(r['status'] == 'pending' for r in item.get('reviews', [])) else 'unknown' if not item.get('reviews') else 'reviewed'
        item['display_negated'] = display_negated(item)
        rows.append(item)
    return {'claims': rows, 'total': total, 'limit': limit, 'offset': offset}


def collect_claims(cur, **filters):
    """Traverse ID-ordered pages inside the caller's repeatable-read snapshot."""
    claims, last_id, expected = [], 0, None
    while True:
        page = fetch_claim_page(cur, limit=500, after_id=last_id, **filters)
        if expected is None:
            expected = page['total']
        rows = page['claims']
        if not rows:
            break
        for row in rows:
            if row['id'] <= last_id:
                raise ValueError('Claims pagination is not strictly ID ordered')
            last_id = row['id']
            claims.append(row)
    if len(claims) != expected:
        raise ValueError('Claims export count differs from snapshot total')
    return {'claims': claims, 'exported_count': len(claims), 'total': expected,
            'document_count':len({c.get('document_id') for c in claims if c.get('document_id') is not None})}


def collect_facts(cur, *, knowledge_base_id, document_id=None, entity_id=None):
    """Export the complete current fact layer, including actual participant IDs."""
    from app.retrieval import fetch_facts
    cur.execute("""SELECT COUNT(*)::INT AS total FROM facts f JOIN claims c ON c.id=f.claim_id
        JOIN documents d ON d.id=c.document_id JOIN knowledge_base_documents m ON m.document_id=c.document_id
        WHERE m.knowledge_base_id=%s AND f.catalog_version IS NOT DISTINCT FROM d.catalog_version
        AND d.catalog_status IN ('done','legacy') AND (%s::BIGINT IS NULL OR c.document_id=%s)
        AND (%s::BIGINT IS NULL OR EXISTS (SELECT 1 FROM fact_participants p WHERE p.fact_id=f.id AND p.entity_id=%s))""",
        (knowledge_base_id,document_id,document_id,entity_id,entity_id))
    total = cur.fetchone()['total']
    facts, last_id = [], 0
    while True:
        rows = fetch_facts(cur, knowledge_base_id, document_id=document_id, entity_id=entity_id, limit=500, after_id=last_id)
        if not rows:
            break
        for row in rows:
            if row['id'] <= last_id:
                raise ValueError('Facts pagination is not strictly ID ordered')
            last_id = row['id']
            row.pop('source_text',None)
            row.update(result_status=result_view(row), display_negated=display_negated(row),
                       semantic_status=(row.get('claim_json') or {}).get('semantic_status','unknown'),
                       source_kind=(row.get('claim_json') or {}).get('source_kind','unknown'))
            row['review_status'] = 'pending' if row['semantic_status'] == 'pending' or any(r['status'] == 'pending' for r in row.get('reviews', [])) else 'unknown' if not row.get('reviews') else 'reviewed'
            facts.append(row)
    if len(facts) != total:
        raise ValueError('Facts export count differs from snapshot total')
    return {'knowledge_base_id':knowledge_base_id,'facts':facts,'exported_count':len(facts),'total':total,
            'document_count':len({f['document_id'] for f in facts})}


def claims_csv(rows):
    output = io.StringIO(newline='')
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ['id', 'claim_json']
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return '\ufeff' + output.getvalue()
