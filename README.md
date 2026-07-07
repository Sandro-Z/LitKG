# LitKG

LitKG is a literature-oriented extraction pipeline scaffold for building a **unified knowledge base** and a **unified knowledge graph** from paper-level LLM outputs.

The core change in this implementation is that article extraction results are no longer stored as isolated per-paper graphs. Every paper is ingested into one SQLite-backed knowledge store. Entities and relations are normalized, deduplicated, merged, and linked to their paper-level provenance evidence.

## Unified model

LitKG stores four main object classes:

| Object | Purpose |
| --- | --- |
| `papers` | Source literature metadata, including DOI/title/source path. |
| `entities` | Global compounds, molecules, proteins, drugs, genes, or other biomedical entities. |
| `relations` | Global subject-predicate-object facts deduplicated across papers. |
| `evidence` | Paper-level evidence sentences/contexts supporting a global relation. |

The unified graph is generated from the global tables:

- node = one canonical entity across all papers;
- edge = one canonical relation across all papers;
- edge provenance = all evidence snippets and source papers supporting the relation.

## Input format

The ingester accepts JSON, JSONL, or a directory containing `.json`/`.jsonl` files. A minimal JSONL record looks like this:

```json
{
  "paper_id": "paper-001",
  "title": "Example EGFR paper",
  "doi": "10.0000/example",
  "entities": [
    {
      "id": "e1",
      "text": "EGFR",
      "type": "GENE",
      "canonical_name": "EGFR",
      "identifiers": {"NCBI": "1956"},
      "aliases": ["ERBB1"]
    },
    {
      "id": "e2",
      "text": "Erlotinib",
      "type": "DRUG",
      "canonical_name": "Erlotinib",
      "identifiers": {"DrugBank": "DB00530"}
    }
  ],
  "relations": [
    {
      "subject": "e2",
      "predicate": "inhibits",
      "object": "e1",
      "evidence": "Erlotinib inhibits EGFR tyrosine kinase activity.",
      "confidence": 0.93
    }
  ]
}
```

`subject` and `object` can be local entity IDs, names, raw strings, or full entity dictionaries. The store resolves them into global entity IDs.

## Usage

Install in editable mode:

```bash
python -m pip install -e .
```

Initialize a unified store:

```bash
litkg init --db data/litkg.sqlite
```

Ingest all extracted paper records into the unified knowledge base:

```bash
litkg ingest --db data/litkg.sqlite --input examples/article_extractions.jsonl
litkg ingest-dir --db data/litkg.sqlite --input-dir extracted_articles/
```

Export the unified graph:

```bash
litkg export-json --db data/litkg.sqlite --out data/unified_graph.json
litkg export-graphml --db data/litkg.sqlite --out data/unified_graph.graphml
```

Inspect store statistics:

```bash
litkg stats --db data/litkg.sqlite
```

## Integration point for an existing LLM extraction pipeline

Wire the current per-paper extraction output into `UnifiedKnowledgeStore.ingest_payload(...)` instead of writing one knowledge base or graph file per article:

```python
from litkg import UnifiedKnowledgeStore

store = UnifiedKnowledgeStore("data/litkg.sqlite")
store.initialize()

for paper_payload in extracted_papers:
    store.ingest_payload(paper_payload, source_path=paper_payload.get("source_path"))

store.export_json("data/unified_graph.json")
store.export_graphml("data/unified_graph.graphml")
```

## Deduplication behavior

Entity identity is resolved in this priority order:

1. external identifiers, such as `NCBI:1956`, `UniProt:P00533`, `DrugBank:DB00530`;
2. canonical name + entity type;
3. raw mention text + entity type.

Relation identity is resolved by:

```text
global_subject_entity + normalized_predicate + global_object_entity
```

Every duplicate relation accumulates evidence instead of creating a new article-scoped edge.
