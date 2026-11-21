"""Read-only, repeatable-read review snapshots for the separate QC service."""
from app.claim_export import collect_claims
from app.entity_types import TYPE_DEFINITIONS, TYPE_PARENTS
from app.predicate_registry import PREDICATES, REGISTRY_VERSION


def collect_quality_snapshot(cur, knowledge_base_id):
    claims = collect_claims(cur, knowledge_base_id=knowledge_base_id)
    cur.execute('''SELECT d.id,d.title,d.sha256,d.status,d.pipeline_generation,d.external_id,
        d.source_path LIKE '%%.litkg-text.json' AS is_text_source
        FROM documents d JOIN knowledge_base_documents m ON m.document_id=d.id
        WHERE m.knowledge_base_id=%s ORDER BY d.id''', (knowledge_base_id,))
    documents = []
    for doc in cur.fetchall():
        cur.execute('''SELECT id,chunk_index,section_title,page_start,page_end,page_spans,text,
            source_block_id,source_offset
            FROM chunks WHERE document_id=%s ORDER BY chunk_index,id''', (doc['id'],))
        chunks = cur.fetchall()
        # Text sources (e.g. benchmark documents) keep their caller-supplied
        # external_id so other tools can align documents across systems. PDF
        # exports stay unchanged when these fields are absent.
        extra = {}
        if doc.get('external_id') is not None:
            extra['external_id'] = doc['external_id']
        if doc.get('is_text_source'):
            extra['source_kind'] = 'text'
        documents.append(dict(id=str(doc['id']), title=doc['title'], source_sha256=doc['sha256'], **extra,
                              # Saved extraction chunks do not establish PDF/figure completeness.
                              parsing_status='partial', generation=doc['pipeline_generation'],
                              blocks=[dict(id=str(c['id']), text=c['text'], page=c['page_start'],
                                           page_end=c['page_end'], section=c['section_title'],
                                           page_spans=c['page_spans'], kind='text',
                                           **({'source_block_id': c['source_block_id'], 'source_offset': c['source_offset']}
                                              if c.get('source_block_id') is not None else {})) for c in chunks]))
    return dict(schema_version='1.0.0', name=f'LitKG KB {knowledge_base_id}',
                claims=claims['claims'], documents=documents,
                policy=dict(scope=None, ontology=dict(node_types=TYPE_DEFINITIONS,
                                                       predicates=PREDICATES, type_parents=TYPE_PARENTS)),
                metadata=dict(source='litkg', knowledge_base_id=knowledge_base_id,
                              registry_version=REGISTRY_VERSION, exported_count=claims['exported_count'],
                              source_coverage='all_saved_chunks; PDF visual regions not verified'))
