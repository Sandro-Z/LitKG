"""Read-only, repeatable-read review snapshots for the separate QC service."""
from app.claim_export import collect_claims
from app.entity_types import TYPE_DEFINITIONS, TYPE_PARENTS
from app.predicate_registry import PREDICATES, REGISTRY_VERSION


def collect_quality_snapshot(cur, knowledge_base_id):
    claims = collect_claims(cur, knowledge_base_id=knowledge_base_id)
    cur.execute('''SELECT d.id,d.title,d.sha256,d.status,d.pipeline_generation
        FROM documents d JOIN knowledge_base_documents m ON m.document_id=d.id
        WHERE m.knowledge_base_id=%s ORDER BY d.id''', (knowledge_base_id,))
    documents = []
    for doc in cur.fetchall():
        cur.execute('''SELECT id,chunk_index,section_title,page_start,page_end,page_spans,text
            FROM chunks WHERE document_id=%s ORDER BY chunk_index,id''', (doc['id'],))
        chunks = cur.fetchall()
        documents.append(dict(id=str(doc['id']), title=doc['title'], source_sha256=doc['sha256'],
                              # Saved extraction chunks do not establish PDF/figure completeness.
                              parsing_status='partial', generation=doc['pipeline_generation'],
                              blocks=[dict(id=str(c['id']), text=c['text'], page=c['page_start'],
                                           page_end=c['page_end'], section=c['section_title'],
                                           page_spans=c['page_spans'], kind='text') for c in chunks]))
    return dict(schema_version='1.0.0', name=f'LitKG KB {knowledge_base_id}',
                claims=claims['claims'], documents=documents,
                policy=dict(scope=None, ontology=dict(node_types=TYPE_DEFINITIONS,
                                                       predicates=PREDICATES, type_parents=TYPE_PARENTS)),
                metadata=dict(source='litkg', knowledge_base_id=knowledge_base_id,
                              registry_version=REGISTRY_VERSION, exported_count=claims['exported_count'],
                              source_coverage='all_saved_chunks; PDF visual regions not verified'))
