"""Neo4j LLM Graph Builder baseline (neo4j-labs/llm-graph-builder, pinned release).

Drives the backend routes found in ``backend/score.py`` of the pinned release
(``/upload`` then ``/extract``), polls the ``Document`` node status in Neo4j,
and exports each document's entities/relations with the official neo4j driver.
Schema notes: ``docs/neo4j_schema_notes.md``.
"""
import base64
import re
import time
from collections import Counter
from pathlib import Path

from ..cache import Cache
from ..common import digest, ontology, sha256_text, type_allowed
from ..graph import GraphBuilder
from ..http import Http
from ..text_prep import build, sentences

STRUCTURAL = ['HAS_ENTITY', 'PART_OF', 'FIRST_CHUNK', 'NEXT_CHUNK', 'SIMILAR', 'IN_COMMUNITY', 'PARENT_COMMUNITY',
              'HAS_CHUNK']
INTERNAL_LABELS = {'__Entity__', '__Community__', 'Chunk', 'Document', 'Session', 'Message'}

Q_STATUS = 'MATCH (d:Document {fileName: $f}) RETURN d.status AS status, d.errorMessage AS error, d.model AS model'
Q_CHUNKS = ('MATCH (d:Document {fileName: $f})<-[:PART_OF]-(c:Chunk) '
            'RETURN c.id AS id, c.text AS text, c.position AS position ORDER BY c.position')
Q_ENTITIES = ('MATCH (d:Document {fileName: $f})<-[:PART_OF]-(c:Chunk)-[:HAS_ENTITY]->(e) '
              'RETURN c.id AS chunk, elementId(e) AS eid, labels(e) AS labels, e.id AS name, '
              'e.description AS description')
Q_RELATIONS = ('MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids AND NOT type(r) IN $structural '
               'RETURN elementId(a) AS a, type(r) AS type, elementId(b) AS b')
Q_RESET = 'MATCH (n) DETACH DELETE n'


class BuilderError(RuntimeError):
    pass


def predicate_instructions(policy):
    """Policy predicates as plain text for the builder's additional_instructions field."""
    predicates, _, _ = ontology(policy)
    lines = ['Use ONLY these relationship types (exact names). Subject/object types in brackets; * means any type.']
    for name, spec in predicates.items():
        subjects = '|'.join(spec.get('subject_types') or ['*'])
        objects = '|'.join(spec.get('object_types') or ['*'])
        lines.append(f'{name} [{subjects} -> {objects}]: {spec.get("definition", "")}')
    lines.append('If no listed type fits, do not create the relationship.')
    return ' ; '.join(lines)


def schema(policy, mode, max_triples):
    """Return (allowedNodes, allowedRelationship, triple_count, schema_mode, additional_instructions).

    'triples': every (source, relationship, target) permitted by the policy's domain/range is
    sent and enforced by the builder. LangChain's LLMGraphTransformer copies that whole list
    into the tool schema of every request, so when it exceeds max_triples (LitKG's ontology
    expands to ~12k triples, ~150k tokens per chunk) the 'nodes+instructions' mode is used:
    node labels stay enforced via allowedNodes, while predicate names, definitions and
    domain/range go into the builder's additional_instructions (guidance, not enforcement).
    """
    predicates, node_types, _ = ontology(policy)
    if mode == 'no-schema':
        return '', '', 0, 'none', ''
    nodes = [t for t in node_types if t != 'Other']
    triples = []
    for name, spec in predicates.items():
        subjects = nodes if '*' in (spec.get('subject_types') or ['*']) else [t for t in spec['subject_types'] if t in nodes]
        objects = nodes if '*' in (spec.get('object_types') or ['*']) else [t for t in spec['object_types'] if t in nodes]
        triples += [f'{s},{name},{o}' for s in subjects for o in objects]
    if len(triples) <= max_triples:
        return ','.join(nodes), ','.join(triples), len(triples), 'triples', ''
    return ','.join(nodes), '', len(triples), 'nodes+instructions', predicate_instructions(policy)


def safe_filename(doc_id):
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', str(doc_id))[:80]
    return f'{stem}-{sha256_text(str(doc_id))[:8]}'


class BuilderClient:
    def __init__(self, cfg, password, http=None):
        self.cfg, self._password = cfg, password
        self.http = http or Http(per_second=5, timeout=None, retries=2)
        self.base = cfg['backend_url'].rstrip('/')

    def _credentials(self):
        # The pinned backend base64-decodes the password field (user_credential._decode_password).
        # backend_uri is the address as seen from the builder container (e.g. bolt://neo4j:7687).
        return {'uri': self.cfg.get('backend_uri') or self.cfg['uri'], 'userName': self.cfg['user'],
                'database': self.cfg['database'],
                'password': base64.b64encode(self._password.encode()).decode()}

    def upload(self, filename, data, model):
        form = dict(self._credentials(), chunkNumber='1', totalChunks='1', originalname=filename, model=model,
                    uploadId=sha256_text(filename)[:16])
        response = self.http.request('POST', f'{self.base}/upload', data=form, files={'file': (filename, data)})
        body = response.json()
        if body.get('status') != 'Success':
            raise BuilderError(f'upload failed for {filename}: {body.get("message")}')

    def extract(self, filename, model, nodes, relationships, additional_instructions=''):
        form = dict(self._credentials(), model=model, source_type='local file', file_name=filename,
                    allowedNodes=nodes, allowedRelationship=relationships,
                    additional_instructions=additional_instructions,
                    token_chunk_size=str(self.cfg['token_chunk_size']), chunk_overlap=str(self.cfg['chunk_overlap']),
                    chunks_to_combine=str(self.cfg['chunks_to_combine']))
        response = self.http.request('POST', f'{self.base}/extract', data=form, retry_status=set())
        body = response.json()
        if body.get('status') != 'Success':
            raise BuilderError(f'extract failed for {filename}: {body.get("message")}')
        return body.get('data') or {}


class Neo4jStore:
    def __init__(self, cfg, password, driver=None):
        if driver is None:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(cfg['uri'], auth=(cfg['user'], password))
        self.driver, self.database = driver, cfg['database']

    def query(self, cypher, **params):
        records, _, _ = self.driver.execute_query(cypher, params, database_=self.database)
        return [r.data() for r in records]

    def close(self):
        self.driver.close()


def surface_forms(name):
    forms = {name} if name else set()
    if name and '_' in name:
        forms.add(name.replace('_', ' '))
    return [f for f in forms if f]


def export_document(store, filename):
    chunks = store.query(Q_CHUNKS, f=filename)
    entities = store.query(Q_ENTITIES, f=filename)
    ids = sorted({e['eid'] for e in entities})
    relations = store.query(Q_RELATIONS, ids=ids, structural=STRUCTURAL) if ids else []
    return chunks, entities, relations


def convert_document(builder, doc, chunks, entities, relations, stats):
    """Entities/relations -> claims; evidence = shortest chunk sentence naming both endpoints."""
    dt = build(doc)
    doc_id = str(doc['id'])
    chunk_text = {c['id']: c['text'] or '' for c in chunks}
    by_eid, chunk_entities = {}, {}
    for e in entities:
        types = [l for l in e['labels'] if l not in INTERNAL_LABELS]
        node_id = f'nb:{"|".join(sorted(types)) or "Entity"}:{e["name"]}'
        policy_types = [t for t in types if t in builder.node_types] or ['Other']
        builder.node(node_id, e['name'], types=policy_types)
        builder.nodes[node_id].setdefault('original_types', [])
        for t in types:
            if t not in builder.nodes[node_id]['original_types']:
                builder.nodes[node_id]['original_types'].append(t)
        by_eid[e['eid']] = (node_id, e['name'])
        chunk_entities.setdefault(e['chunk'], set()).add(e['eid'])
    for r in relations:
        a, b = by_eid.get(r['a']), by_eid.get(r['b'])
        if not a or not b:
            stats['relation_endpoint_outside_document'] += 1
            continue
        shared = [cid for cid, eids in chunk_entities.items() if r['a'] in eids and r['b'] in eids]
        quote, hint = None, None
        forms_a, forms_b = surface_forms(a[1]), surface_forms(b[1])
        best = None
        for cid in shared:
            text = chunk_text.get(cid, '')
            for s, e in sentences(text):
                sentence = text[s:e].casefold()
                if any(f.casefold() in sentence for f in forms_a) and any(f.casefold() in sentence for f in forms_b):
                    if best is None or e - s < len(best):
                        best = text[s:e]
        if best:
            position = dt.text.find(best)
            if position >= 0:
                hint = (dt.locate(position, position + len(best)) or [None])[0]
            quote = best
        else:
            stats['no_cosentence'] += 1
        builder.claim(a[0], r['type'], b[0], doc_id, quote=quote, hint_block=hint,
                      evidence_method='neo4j_builder_chunk_cosentence')


def run(documents, policy, cfg, variant, client, store, pdf_dir=None, sleep=time.sleep, cache_root=None, pdf_index=None):
    if variant not in {'sametext', 'pdfnative', 'no-schema'}:
        raise BuilderError(f'unknown variant {variant}')
    if not cfg.get('dedicated_database'):
        raise BuilderError('neo4j.dedicated_database must be true: documents are isolated by clearing the database '
                           'between documents, which deletes everything in it')
    model = cfg.get('model')
    if not model:
        raise BuilderError('neo4j.model (the LLM_MODEL_CONFIG_* key known to the backend) is required')
    nodes, relationships, triple_count, schema_mode, instructions = schema(
        policy, 'no-schema' if variant == 'no-schema' else 'schema', cfg.get('max_schema_triples', 1000))
    builder = GraphBuilder('Neo4j LLM Graph Builder', 'nb', documents, policy)
    stats = Counter()
    # Per-document checkpoint: a finished export is reused, so an interrupted run resumes
    # at the first unfinished document without new LLM calls.
    checkpoints = Cache(cache_root, 'neo4j-builder') if cache_root else None
    settings = digest([variant, nodes, relationships, instructions, model, cfg.get('model_name'), cfg.get('token_chunk_size'),
                       cfg.get('chunk_overlap'), cfg.get('chunks_to_combine'), cfg.get('builder_commit')])
    for doc in documents:
        name = safe_filename(doc['id'])
        key = sha256_text(f'{settings}\0{digest(doc)}')
        saved = checkpoints.get(key) if checkpoints else None
        if saved is not None:
            stats['documents_from_checkpoint'] += 1
            stats['chunks'] += len(saved['chunks'])
            convert_document(builder, doc, saved['chunks'], saved['entities'], saved['relations'], stats)
            continue
        if variant == 'pdfnative':
            indexed = (pdf_index or {}).get(doc.get('source_sha256'))
            pdf = Path(indexed) if indexed else Path(pdf_dir or 'pdfs') / f'{doc.get("source_sha256")}.pdf'
            if not doc.get('source_sha256') or not pdf.is_file():
                stats['pdf_missing'] += 1
                continue
            filename, data = f'{name}.pdf', pdf.read_bytes()
        else:
            filename, data = f'{name}.txt', build(doc).text.encode('utf-8')
        store.query(Q_RESET)
        client.upload(filename, data, model)
        client.extract(filename, model, nodes, relationships, instructions)
        waited = 0
        while True:
            status = (store.query(Q_STATUS, f=filename) or [{}])[0]
            if status.get('status') in {'Completed', 'Failed', 'Cancelled'}:
                break
            if waited >= cfg.get('max_wait', 3600):
                status = {'status': 'Timeout'}
                break
            sleep(cfg.get('poll_interval', 10))
            waited += cfg.get('poll_interval', 10)
        stats[f'document_status_{status.get("status")}'] += 1
        if status.get('status') != 'Completed':
            continue
        chunks, entities, relations = export_document(store, filename)
        if checkpoints:
            checkpoints.put(key, dict(chunks=chunks, entities=entities, relations=relations))
        stats['chunks'] += len(chunks)
        convert_document(builder, doc, chunks, entities, relations, stats)
    store.query(Q_RESET)
    # Domain/range is only guidance in nodes+instructions mode: count violations, keep the claims.
    _, _, parents = ontology(policy)
    predicates = ontology(policy)[0]
    for claim in builder.claims:
        spec = predicates.get(claim['predicate'])
        if spec is None:
            continue
        s_types = builder.nodes[claim['subject']['id']]['types']
        o_types = builder.nodes[claim['object']['id']]['types']
        if not (any(type_allowed(t, spec.get('subject_types') or ['*'], parents) for t in s_types) and
                any(type_allowed(t, spec.get('object_types') or ['*'], parents) for t in o_types)):
            stats['domain_range_violations'] += 1
    return builder, dict(stats=dict(stats), schema_mode=schema_mode, additional_instructions_sha256=
                         digest(instructions) if instructions else None,
                         schema_sha256=digest([nodes, relationships, instructions]), schema_triples=triple_count,
                         input='original PDF' if variant == 'pdfnative' else 'kgb same-text .txt')
