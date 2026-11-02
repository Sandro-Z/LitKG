#!/usr/bin/env python3
"""Read-only, framework-neutral tools for the bundled KG review contracts.

One JSON invocation on stdin, one JSON envelope on stdout. Uses only stdlib.
The schema checker deliberately implements only the subset used in this bundle.
It is not a general JSON Schema or SHACL engine and does not assess entailment.
"""
import collections
import hashlib
import json
import random
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "kg-review/1.0.0"
VERDICTS = ("SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED", "UNSUPPORTED",
            "INSUFFICIENT_EVIDENCE", "SOURCE_CONFLICT")


class ToolError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def schema_errors(value, schema, root=None, path="$"):
    """Support $ref, type, const, enum, object/array and scalar bounds used here."""
    root = schema if root is None else root
    if "$ref" in schema:
        target = root
        for key in schema["$ref"].removeprefix("#/").split("/"):
            target = target[key]
        return schema_errors(value, target, root, path)
    errors = []
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    tests = {"null": value is None, "boolean": isinstance(value, bool),
             "string": isinstance(value, str), "object": isinstance(value, dict),
             "array": isinstance(value, list),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if types and not any(tests[t] for t in types):
        return [f"{path}: expected {types}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: incorrect constant")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not an allowed enum value")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing")
        for key, item in value.items():
            if key in props:
                errors += schema_errors(item, props[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unknown field")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += schema_errors(item, schema["additionalProperties"], root, f"{path}.{key}")
    if isinstance(value, list):
        for i, item in enumerate(value):
            errors += schema_errors(item, schema.get("items", {}), root, f"{path}[{i}]")
        for key, compare in (("minItems", len(value) < schema.get("minItems", 0)),
                             ("maxItems", len(value) > schema.get("maxItems", float("inf")))):
            if compare:
                errors.append(f"{path}: {key}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            errors.append(f"{path}: string length out of range")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            errors.append(f"{path}: number out of range")
    return errors


def load_request(path):
    request = read_json(path)
    errors = schema_errors(request, read_json(ROOT / "assets/request.schema.json"))
    if errors:
        raise ToolError("REQUEST_SCHEMA: " + "; ".join(errors[:20]))
    return request


def snapshots(request):
    return {"graph_hash": digest(request["graph"]), "corpus_hash": digest(request["documents"]),
            "policy_hash": digest(request["policy"])}


def all_blocks(request):
    return {(d["id"], b["id"]): b for d in request["documents"] for b in d["blocks"]}


def resolve_block(request, doc_id, block_id):
    docs = [d for d in request["documents"] if d["id"] == doc_id]
    blocks = [b for d in docs for b in d["blocks"] if b["id"] == block_id]
    if len(docs) != 1 or len(blocks) != 1:
        raise ToolError(f"SOURCE_NOT_UNIQUE_OR_MISSING: {doc_id}/{block_id}")
    return docs[0], blocks[0]


def verify(request, evidence):
    try:
        _, block = resolve_block(request, evidence["doc_id"], evidence["block_id"])
        start, end = evidence["start"], evidence["end"]
        valid = 0 <= start < end <= len(block["text"]) and block["text"][start:end] == evidence["quote"]
        return {"valid": valid, "reason": "exact_match" if valid else "offset_or_quote_mismatch",
                "block_hash": digest(block)}
    except ToolError as exc:
        return {"valid": False, "reason": str(exc), "block_hash": None}


def finding(rule, kind, target, severity, path, summary, status="confirmed"):
    return {"id": "F-" + digest([rule, kind, target, path, summary])[:12],
            "rule_id": rule, "target_type": kind, "target_id": target,
            "severity": severity, "status": status, "graph_path": path,
            "summary": summary, "evidence": [], "suggested_fix": "核对原始映射后提出最小修复。"}


def structure(request):
    findings = []
    def emit(*args, **kwargs): findings.append(finding(*args, **kwargs))
    groups = [("node", request["graph"]["nodes"], "/graph/nodes"),
              ("claim", request["graph"]["claims"], "/graph/claims"),
              ("document", request["documents"], "/documents")]
    for kind, items, path in groups:
        seen = set()
        for i, item in enumerate(items):
            if item["id"] in seen:
                emit("ID-01", kind, item["id"], "blocker", f"{path}/{i}/id", "重复 ID，无法唯一寻址。")
            seen.add(item["id"])
    for di, doc in enumerate(request["documents"]):
        seen = set()
        for bi, block in enumerate(doc["blocks"]):
            if block["id"] in seen:
                emit("ID-01", "document", doc["id"], "blocker", f"/documents/{di}/blocks/{bi}/id", "文献内重复 block ID。")
            seen.add(block["id"])
    nodes = {n["id"]: n for n in request["graph"]["nodes"]}
    ontology = request["policy"]["ontology"]
    predicates = {p["name"]: p for p in ontology["predicates"]} if ontology else {}
    if ontology:
        for i, node in enumerate(request["graph"]["nodes"]):
            if node["type"] is not None and node["type"] not in ontology["node_types"]:
                emit("ONT-01", "node", node["id"], "medium", f"/graph/nodes/{i}/type", "类型不在显式本体词表。")
    signatures = {}
    for i, claim in enumerate(request["graph"]["claims"]):
        path = f"/graph/claims/{i}"
        for key, target in [("subject", claim["subject"])] + ([("object/value", claim["object"]["value"])] if claim["object"]["kind"] == "entity" else []):
            if target not in nodes:
                emit("ID-01", "claim", claim["id"], "blocker", path + "/" + key, "悬空实体引用。")
        if not claim["source_refs"]:
            emit("PROV-01", "claim", claim["id"], "low", path + "/source_refs", "原图无证据映射；先尝试从文献恢复。")
        for ri, evidence in enumerate(claim["source_refs"]):
            if not verify(request, evidence)["valid"]:
                emit("PROV-01", "claim", claim["id"], "medium", path + f"/source_refs/{ri}", "原始来源锚点无法精确验证。")
        signature = digest({k: claim[k] for k in ["subject", "predicate", "object", "qualifiers"]})
        if signature in signatures:
            emit("DUP-01", "claim", claim["id"], "low", path, "语义字段精确重复：" + signatures[signature] + "；保留并合并来源的建议待复核。")
        signatures[signature] = claim["id"]
        if ontology and claim["predicate"] not in predicates:
            emit("ONT-01", "claim", claim["id"], "medium", path + "/predicate", "谓词不在显式本体词表。")
        rule = predicates.get(claim["predicate"])
        if rule:
            checks = [(claim["subject"], rule["domain"], "subject")]
            if claim["object"]["kind"] == "entity":
                checks.append((claim["object"]["value"], rule["range"], "object"))
            for nid, allowed, part in checks:
                if nid in nodes and allowed:
                    t = nodes[nid]["type"]
                    if t is None:
                        emit("ONT-01", "claim", claim["id"], "low", path + "/" + part, "实体类型未知，无法验证类型约束。", status="hypothesis")
                    elif t not in allowed:
                        emit("ONT-01", "claim", claim["id"], "medium", path + "/" + part, "违反用户显式声明的 domain/range 类型约束。")
    return {"findings": findings, "checks": ["unique_ids", "references", "exact_evidence", "exact_duplicates", "explicit_entity_types"]}


def compute(request, report):
    assessments = {a["claim_id"]: a for a in report["assessments"]}
    counts = dict.fromkeys(VERDICTS, 0)
    for a in assessments.values(): counts[a["verdict"]] += 1
    n, r = len(request["graph"]["claims"]), len(assessments)
    blocks = set(all_blocks(request))
    read = {(x["doc_id"], x["block_id"]) for x in report["document_coverage"]} & blocks
    omission_read = {(x["doc_id"], x["block_id"]) for x in report["document_coverage"] if x["purpose"] in ("omission", "both")} & blocks
    statuses = collections.Counter(c["match_status"] for c in report["omission_candidates"])
    eligible = statuses["matched"] + statuses["misrepresented"] + statuses["missing"]
    confirmed = [f for f in report["findings"] if f["status"] == "confirmed"]
    unresolved = counts["INSUFFICIENT_EVIDENCE"] + counts["SOURCE_CONFLICT"]
    incomplete = (not n or not blocks or r < n or len(read) < len(blocks) or len(omission_read) < len(blocks)
                  or not request["policy"]["scope"] or unresolved or statuses["ambiguous"]
                  or any(d["parsing_status"] != "complete" for d in request["documents"])
                  or report["unreviewed_regions"] or any(f["status"] == "hypothesis" for f in report["findings"]))
    overall = ("FAIL" if any(f["severity"] == "blocker" for f in confirmed) else
               "NEEDS_REVISION" if any(f["severity"] in ("high", "medium") for f in confirmed) else
               "INCOMPLETE" if incomplete else "PASS")
    rate = lambda a, b: a / b if b else None
    return {"claims_total": n, "claims_reviewed": r, "verdict_counts": counts,
            "claim_coverage": rate(r, n), "strict_support_rate_reviewed": rate(counts["SUPPORTED"], r),
            "unresolved_rate_reviewed": rate(unresolved, r), "blocks_total": len(blocks),
            "blocks_reviewed": len(read), "block_coverage": rate(len(read), len(blocks)),
            "omission_blocks_reviewed": len(omission_read), "omission_block_coverage": rate(len(omission_read), len(blocks)),
            "omission_matched": statuses["matched"], "omission_misrepresented": statuses["misrepresented"], "omission_missing": statuses["missing"],
            "omission_ambiguous": statuses["ambiguous"],
            "candidate_hit_rate": rate(statuses["matched"], eligible) if request["policy"]["scope"] else None,
            "overall": overall}


def report_errors(request, report):
    errors = schema_errors(report, read_json(ROOT / "assets/report.schema.json"))
    if errors: return errors
    claims = {c["id"]: c for c in request["graph"]["claims"]}
    nodes = {n["id"] for n in request["graph"]["nodes"]}
    docs = {d["id"]: d for d in request["documents"]}
    blocks = set(all_blocks(request))
    for group, field in [("assessments", "claim_id"), ("findings", "id"), ("omission_candidates", "id")]:
        ids = [x[field] for x in report[group]]
        if len(set(ids)) != len(ids): errors.append(f"{group}: duplicate {field}")
    if len({(x['doc_id'], x['block_id']) for x in report['document_coverage']}) != len(report['document_coverage']):
        errors.append("duplicate document_coverage entry")
    for key, value in snapshots(request).items():
        if report["run"][key] != value: errors.append("snapshot mismatch: " + key)
    def check_ev(items, owner):
        for ev in items:
            if not verify(request, ev)["valid"]: errors.append(owner + ": invalid evidence anchor")
    for a in report["assessments"]:
        cid, verdict = a["claim_id"], a["verdict"]
        if cid not in claims: errors.append("unknown claim: " + cid)
        checked = {(b["doc_id"], b["block_id"]) for b in a["searched_blocks"]}
        if not checked.issubset(blocks): errors.append(cid + ": unknown searched block")
        check_ev(a["evidence"], cid)
        if any((e['doc_id'], e['block_id']) not in checked for e in a['evidence']):
            errors.append(cid + ": evidence outside declared inspected blocks")
        roles = {e["role"] for e in a["evidence"]}
        if verdict in ("SUPPORTED", "PARTIALLY_SUPPORTED") and "supports" not in roles:
            errors.append(cid + ": missing supporting evidence")
        if verdict == "CONTRADICTED" and "contradicts" not in roles:
            errors.append(cid + ": missing contradictory evidence")
        if verdict == "SOURCE_CONFLICT" and (not {"supports", "contradicts"}.issubset(roles) or len({e['doc_id'] for e in a['evidence']}) < 2):
            errors.append(cid + ": conflict needs opposing evidence from two documents")
        if verdict == "UNSUPPORTED":
            source_docs = {e["doc_id"] for e in claims.get(cid, {}).get("source_refs", [])} or set(docs)
            if not checked or not a["search_notes"] or any(docs.get(d, {}).get('parsing_status') != 'complete' for d in source_docs):
                errors.append(cid + ": unsupported label lacks readable sources or search record")
        if verdict in ("PARTIALLY_SUPPORTED", "CONTRADICTED", "UNSUPPORTED") and not any(f["target_type"] == "claim" and f["target_id"] == cid and f["status"] == "confirmed" for f in report["findings"]):
            errors.append(cid + ": semantic error has no confirmed finding")
    candidates = {c["id"]: c for c in report["omission_candidates"]}
    for c in candidates.values():
        check_ev(c["evidence"], c["id"])
        if not c["evidence"]: errors.append(c["id"] + ": candidate needs source evidence")
        if not set(c["matched_claim_ids"]).issubset(claims): errors.append(c["id"] + ": unknown matched claim")
        if (c["match_status"] in ("matched", "misrepresented")) != bool(c["matched_claim_ids"]): errors.append(c["id"] + ": invalid match ID state")
        if c["match_status"] == "misrepresented" and not any(f["target_type"] == "claim" and f["target_id"] in c["matched_claim_ids"] and f["status"] == "confirmed" for f in report["findings"]):
            errors.append(c["id"] + ": misrepresented candidate needs an existing claim finding")
        if c["match_status"] == "missing" and request["policy"]["scope"] and not any(f["rule_id"] == "MISS-01" and f["target_id"] == c["id"] and f["status"] == "confirmed" for f in report["findings"]):
            errors.append(c["id"] + ": scoped omission needs confirmed MISS-01 finding")
    targets = {"node": nodes, "claim": set(claims), "document": set(docs), "candidate": set(candidates), "graph": {"graph"}}
    for f in report["findings"]:
        if f["target_id"] not in targets[f["target_type"]]: errors.append(f["id"] + ": unknown target")
        check_ev(f["evidence"], f["id"])
        if f['graph_path'] is not None:
            try:
                pointer = f['graph_path']
                if not pointer.startswith('/'): raise ValueError('not a JSON pointer')
                current = request
                parts = [p.replace('~1','/').replace('~0','~') for p in pointer[1:].split('/')]
                for part in parts:
                    current = current[int(part)] if isinstance(current,list) else current[part]
                prefix = {'claim':['graph','claims'], 'node':['graph','nodes'], 'document':['documents']}.get(f['target_type'])
                if prefix:
                    if parts[:len(prefix)] != prefix: raise ValueError('target/path mismatch')
                    items = request['graph'][prefix[-1]] if prefix[0]=='graph' else request['documents']
                    if items[int(parts[len(prefix)])]['id'] != f['target_id']: raise ValueError('target/path mismatch')
            except (KeyError, ValueError, IndexError, TypeError):
                errors.append(f['id'] + ': invalid graph_path or target/path mismatch')
        if f["rule_id"] == "MISS-01" and f["status"] == "confirmed" and not request["policy"]["scope"]:
            errors.append(f["id"] + ": cannot confirm omission without scope")
    for f in structure(request)["findings"]:
        if f["severity"] in ("blocker", "high", "medium") and f["status"] == "confirmed" and not any(x["rule_id"] == f["rule_id"] and x["target_id"] == f["target_id"] and x["graph_path"] == f["graph_path"] and x["status"] == "confirmed" for x in report["findings"]):
            errors.append("unreported structural violation: " + f["graph_path"])
    for c in report["document_coverage"]:
        if (c["doc_id"], c["block_id"]) not in blocks: errors.append("unknown coverage block")
    for p in report["proposals"]:
        check_ev(p["evidence"], "proposal")
        if not p["preconditions"] or not p["finding_ids"] or not set(p["finding_ids"]).issubset({f['id'] for f in report['findings']}):
            errors.append("proposal needs known findings and preconditions")
        if p['operation'] in ('add_node', 'add_claim', 'replace_claim') and not p['evidence']:
            errors.append("new/changed facts require evidence")
    if report["metrics"] != compute(request, report): errors.append("metrics or gate mismatch")
    return errors


def dispatch(name, args):
    definitions = {d['name']: d for d in read_json(ROOT / 'assets/tool-definitions.json')}
    if name not in definitions: raise ToolError("UNKNOWN_TOOL")
    errors = schema_errors(args, definitions[name]['parameters'])
    if errors: raise ToolError("ARGUMENT_SCHEMA: " + "; ".join(errors))
    request = load_request(args['request_path'])
    if name == 'inventory':
        return {**snapshots(request), 'claims':len(request['graph']['claims']), 'nodes':len(request['graph']['nodes']),
                'documents':[{'id':d['id'],'title':d['title'],'parsing_status':d['parsing_status'],'blocks':len(d['blocks'])} for d in request['documents']],
                'scope_known':bool(request['policy']['scope']), 'ontology_present':request['policy']['ontology'] is not None}
    if name == 'validate_structure': return structure(request)
    if name == 'verify_evidence': return verify(request, args['evidence'])
    if name == 'read_block':
        doc, block = resolve_block(request, args['doc_id'], args['block_id'])
        return {'doc_id':doc['id'], **block, 'block_hash':digest(block)}
    if name == 'get_claims':
        offset, limit = args.get('offset',0), args.get('limit',50)
        claims = request['graph']['claims']; batch = claims[offset:offset+limit]
        ids = {c['subject'] for c in batch} | {c['object']['value'] for c in batch if c['object']['kind'] == 'entity'}
        return {'total':len(claims), 'claims':batch,'nodes':[n for n in request['graph']['nodes'] if n['id'] in ids],
                'next_offset':offset+len(batch) if offset+len(batch)<len(claims) else None}
    if name == 'search_sources':
        norm = lambda s: unicodedata.normalize('NFKC',s).casefold()
        tokens = norm(args['query']).split()
        if not tokens: raise ToolError('EMPTY_QUERY')
        results=[]; searched=0
        for doc in request['documents']:
            if args.get('doc_ids') and doc['id'] not in args['doc_ids']: continue
            for block in doc['blocks']:
                searched+=1; text=norm(block['text']); score=sum(t in text for t in set(tokens))
                if score: results.append({'doc_id':doc['id'],'block_id':block['id'],'matched_terms':score,'snippet':block['text'][:500]})
        results.sort(key=lambda x:(-x['matched_terms'],x['doc_id'],x['block_id']))
        return {'searched_blocks_count':searched,'matches_total':len(results),'results':results[:args.get('limit',20)],'method':'lexical discovery only; read full blocks before judging'}
    if name == 'plan_review':
        ids=sorted(c['id'] for c in request['graph']['claims'])
        if len(ids)!=len(set(ids)): raise ToolError('DUPLICATE_CLAIM_IDS: repair addressing first')
        mode=args.get('mode','all'); seed=args.get('seed','kg-review-v1')
        selected=ids if mode=='all' else sorted(random.Random(seed).sample(ids,min(args.get('sample_size',50),len(ids))))
        return {'mode':mode,'seed':seed,'selected_claim_ids':selected,'unreviewed_claim_ids':sorted(set(ids)-set(selected)),
                'note':('All canonical claim IDs selected; document and semantic completion are tracked separately.' if mode=='all' else
                        'Deterministic pseudorandom sample; report observed sample only, no population estimate.')}
    report=read_json(args['report_path'])
    if name=='compute_metrics': return compute(request, report)
    errors=report_errors(request, report)
    return {'valid':not errors,'errors':errors,'semantic_entailment_checked':False,'tool_trace_checked':False}


def main():
    try:
        call=json.load(sys.stdin)
        if set(call)!={'name','arguments'}: raise ToolError('Expected name and arguments only')
        result=dispatch(call['name'],call['arguments'])
        print(json.dumps({'ok':True,'result':result},ensure_ascii=False,allow_nan=False))
    except (ToolError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'ok':False,'error':{'code':type(exc).__name__,'message':str(exc)}},ensure_ascii=False))
        sys.exit(1)


if __name__=='__main__': main()
