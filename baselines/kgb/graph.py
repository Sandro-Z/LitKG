"""Builds the shared output contract (QC canonical graph) for every pipeline."""
from collections import Counter

from .anchor import anchor
from .common import digest, map_entity_type, now, ontology

APPLIED_REVIEW = {'confirmed', 'needs_review'}


class GraphBuilder:
    def __init__(self, system, prefix, documents, policy, *, entity_type_map=None, relation_map=None):
        self.system, self.prefix = system, prefix
        self.documents, self.policy = documents, policy
        self.docs = {str(d['id']): d for d in documents}
        self.predicates, self.node_types, self.parents = ontology(policy)
        self.entity_type_map = entity_type_map or {}
        self.relation_map = relation_map or {}
        self.nodes, self.claims = {}, []
        self.stats = Counter()
        self.unmapped = Counter()
        self.unanchored = []
        self.drops = Counter()

    # ---- nodes
    def node(self, node_id, label, raw_type=None, *, types=None, aliases=(), xrefs=()):
        if types is None:
            types = [map_entity_type(raw_type, self.entity_type_map, self.node_types)] if raw_type is not None else ['Other']
        if node_id not in self.nodes:
            self.nodes[node_id] = dict(id=node_id, label=label, types=list(types), aliases=[], xrefs=[],
                                       _labels=Counter())
            if raw_type is not None:
                self.nodes[node_id]['original_types'] = []
        n = self.nodes[node_id]
        n['_labels'][label] += 1
        for t in types:
            if t not in n['types']:
                n['types'].append(t)
        if raw_type is not None and raw_type not in n.get('original_types', []):
            n.setdefault('original_types', []).append(raw_type)
        for a in [label, *aliases]:
            if a and a not in n['aliases']:
                n['aliases'].append(a)
        for x in xrefs:
            if x and x not in n['xrefs']:
                n['xrefs'].append(x)
        return node_id

    # ---- predicates
    def map_predicate(self, original):
        """Return (predicate, qualifiers, mapped?) using the mapping file, never guessing."""
        if original in self.predicates:
            return original, {}, True
        rule = self.relation_map.get(original) or self.relation_map.get(str(original).casefold())
        if rule and rule.get('review') in APPLIED_REVIEW and rule.get('predicate') in self.predicates:
            return rule['predicate'], dict(rule.get('qualifiers') or {}), True
        self.unmapped[str(original)] += 1
        return str(original), {}, False

    # ---- claims
    def claim(self, subject, original_predicate, obj, document_id, *, quote=None, hint_block=None, qualifiers=None,
              evidence_method=None, extra=None, predicate=None):
        if predicate is None:
            predicate, mapped_qualifiers, _ = self.map_predicate(original_predicate)
        else:
            mapped_qualifiers = {}
        q = dict(mapped_qualifiers)
        q.update(qualifiers or {})
        q['original_predicate'] = original_predicate
        if evidence_method:
            q['evidence_method'] = evidence_method
        refs = []
        if quote:
            ref, reason = anchor(self.docs[str(document_id)], quote, hint_block)
            if ref:
                ref.pop('match', None)
                refs.append(ref)
                self.stats['anchored'] += 1
            else:
                self.stats['unanchored'] += 1
                self.unanchored.append(dict(document_id=str(document_id), reason=reason, quote=quote[:300]))
        else:
            self.stats['unanchored'] += 1
            self.unanchored.append(dict(document_id=str(document_id), reason='no_evidence_sentence', quote=None))
        claim = dict(id=f'{self.prefix}-{len(self.claims) + 1:06d}', subject={'id': subject}, predicate=predicate,
                     object={'id': obj}, qualifiers=q, document_id=str(document_id), source_refs=refs,
                     confidence=None)
        claim.update(extra or {})
        self.claims.append(claim)
        return claim

    def result(self, name, metadata):
        nodes = []
        for n in self.nodes.values():
            n = dict(n)
            labels = n.pop('_labels')
            n['label'] = sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            nodes.append(n)
        stats = dict(documents=len(self.documents), nodes=len(nodes), claims=len(self.claims),
                     anchored=self.stats['anchored'], unanchored=self.stats['unanchored'],
                     anchor_rate=self.stats['anchored'] / len(self.claims) if self.claims else None,
                     unmapped_predicates=dict(self.unmapped.most_common()),
                     entity_types=dict(Counter(t for n in nodes for t in n['types']).most_common()),
                     drops=dict(self.drops), unanchored_samples=self.unanchored[:200])
        stats.update(metadata.pop('stats', {}))
        meta = dict(system=self.system, corpus_sha256=digest(self.documents), created_at=now(), **metadata, stats=stats)
        return dict(name=name, nodes=nodes, claims=self.claims, documents=self.documents, policy=self.policy,
                    metadata=meta)
