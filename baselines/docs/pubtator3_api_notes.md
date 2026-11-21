# PubTator3 API notes (verified 2026-11-21)

Source: official page https://www.ncbi.nlm.nih.gov/research/pubtator3/api (read on 2026-11-21) plus a few small probe requests that used the page's own example sentence and one public PMID. Everything below is either quoted in substance from that page or observed directly; items marked **unverified** could not be confirmed.

## Rate limit

- The page asks users to post **no more than three requests per second**. `kgb` enforces at most 3 req/s through a shared limiter, retries 429/5xx/timeouts with exponential backoff and jitter (`kgb/http.py`), and caches every response under `cache/`.
- Heavy or large-scale use: the page asks users to contact the maintainers in advance.

## Free-text annotation ("Process Raw Text")

- Submit: `POST https://www.ncbi.nlm.nih.gov/CBBresearch/Lu/Demo/RESTful/request.cgi`, form-encoded `text=<text>&bioconcept=<Bioconcept>`. The response is a session id; observed body: `{"id":"<20 hex chars>"}`.
- Retrieve: `POST https://www.ncbi.nlm.nih.gov/CBBresearch/Lu/Demo/RESTful/retrieve.cgi`, form-encoded `id=<session>`. Until the result is ready it returns **HTTP 404** with “[Warning] : The Result is not ready”.
- Observed output: the PubTator text format. The submitted text becomes the title line (`00000|t|<text>`), the abstract is `-NoAbstract-`, and annotation rows are `00000<TAB>start<TAB>end<TAB>mention<TAB>Type<TAB>Identifier`. Offsets are relative to the submitted text.
- **Relations**: the page describes this endpoint only as running NER tools, and the probe output had **no relation rows**. `kgb` still parses relation rows if any appear (`evidence_method = pubtator_free_text`) and counts them in `stats.freetext_relation_rows`.
- **Retrieve status codes (observed deviation from the docs)**: on 2026-11-21, `retrieve.cgi` answered **HTTP 400 with an HTML error page** while a session was still being processed, not the documented 404. In a live run over 3 BioRED abstracts with `bioconcept=All`, the three sessions returned 400 for 26 polls in total (15 s apart) and then HTTP 200 with results; the whole run took 446 s. `kgb` therefore treats 400 as "pending" for at most `max_wait_400` seconds (default 300). After that it counts `freetext_rejected_<bioconcept>` / `freetext_failed_<bioconcept>`, clears the session, and the run continues.
- **Bioconcept values**: the page only shows `Gene` in its example. `bioconcept=All` returned Gene, Disease, Chemical, Species and Mutation annotations in the live run (55 mentions in 3 abstracts). An earlier single-sentence probe with `All` returned only a Gene mention, so coverage per type is not guaranteed. Separate `Gene`/`Chemical`/`Disease` sessions only produced 400 responses within the ~2 minutes probed (possibly still pending). Default: `bioconcepts = ["All"]`. Check the entity type distribution in `out/stats.md` after every run.
- An HTML body with HTTP 200 is treated as an error: `kgb` clears the pending session and resubmits on the next run.
- **Text length limit**: not documented (**unverified**). `kgb` splits the input at sentence boundaries into pieces of at most `pubtator.max_chars` (default 3000) and shifts offsets back. Newlines and tabs are replaced by spaces of the same length before submission, because the line format cannot carry them; offsets therefore stay valid.

## Export (precomputed annotations and relations for PubMed/PMC articles)

- `GET https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/{pubtator|biocxml|biocjson}?pmids=<comma-separated>`; `full=true` for full texts (BioC formats only); `.../publications/pmc_export/{format}?pmcids=...`.
- Observed BioC-JSON shape: `{"PubTator3": [ {pmid, pmcid, passages:[{offset,text,annotations:[{infons:{identifier,type,name,...}, text, locations:[{offset,length}]}]}], relations:[{id, infons:{type, score, role1:{identifier,type,name,...}, role2:{...}}}]} ]}`.
- Relations are **document-level**; `kgb` takes as evidence the shortest sentence of the input text that names both entities (surface forms from free-text NER plus the export names). If no such sentence exists, `source_refs` is `[]` and the claim is counted as unanchored.

## Relation type sets (two different vocabularies)

- `/relations` search API (page text): `treat, cause, cotreat, convert, compare, interact, associate, positive_correlate, negative_correlate, prevent, inhibit, stimulate, drug_interact`.
- BioC-JSON export `relations[].infons.type` (observed for PMID 15485686): `Association, Positive_Correlation, Negative_Correlation`, which is the BioRED label set. Other BioRED labels (`Bind, Cotreatment, Comparison, Conversion, Drug_Interaction`) are mapped defensively.
- Both vocabularies are listed in `mappings/pubtator3_relations.json`, and each rule has a `review` state. `TODO` rules are not applied; the claim keeps the original predicate, and `qualifiers.original_predicate` is always present.

## Identifiers

- Observed: Gene `6331` (bare NCBI Gene), Species bare NCBI Taxonomy, Chemical/Disease `MESH:D…`, Variant `HGVS:p.V1763M;CorrespondingGene:6331` (export) or tmVar strings (free text).
- `kgb` stores namespaced `xrefs`: `NCBIGene:<n>`, `NCBITaxon:<n>`, `MESH:…` as is, variants as `tmVar:<identifier>` plus `dbSNP:rs<n>` when an `RS#` is present. The node id is `p3:` followed by the xrefs; mentions without an identifier get `p3:unnormalized:<Type>:<hash>` with no xrefs.

## Caveat for gold-standard evaluation

PubTator3's relation extractor (BioREx, listed under “Powered by” on the page) is trained on BioRED data. Precomputed relations for BioRED PMIDs may therefore be contaminated: they can reproduce training annotations. Treat `pubtator3-native` scores on BioRED as an optimistic reference, not as a fair baseline.
