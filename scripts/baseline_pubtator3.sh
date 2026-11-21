#!/usr/bin/env bash
# PubTator3 baseline: PDFs + one LLM API -> graphs in LitKG QC canonical JSON.
#
#   scripts/baseline_pubtator3.sh --pdfs <PDF目录> --llm-base-url <http://host:port/v1> --llm-model <模型>
#   scripts/baseline_pubtator3.sh status | logs | stop
#
# Produces data/baselines/pubtator3/out/graph.pubtator3-native.json (PubTator NER + PubTator
# precomputed relations for PDFs whose DOI resolves to a PMID) and graph.pubtator3-llm.json
# (PubTator entities + relations extracted by the given LLM with the LitKG predicate list).
# Runs in the background; rerun the same command to resume (PubTator/LLM responses are cached).
# Options: --name <语料名> --detach (do not watch) --force (rebuild outputs from caches).
# LLM key: $LLM_API_KEY or a hidden prompt, kept in <work>/secrets/llm_api_key (mode 600).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${KGB_WORK:-$ROOT/data/baselines/pubtator3}"
SYSTEM=pubtator3
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
source "$ROOT/baselines/lib.sh"

prepare_config() { write_config "$BASE_URL" "$MODEL" "$NAME"; }

worker() {
  local force="$1" v
  log "PubTator3 baseline started"
  corpus_step
  kgb prep
  for v in native llm; do
    if [[ -s "$WORK/out/graph.pubtator3-$v.json" && "$force" != 1 ]]; then
      log "pubtator3-$v: already generated (use --force to rebuild from caches)"; continue
    fi
    log "pubtator3-$v: running"
    kgb run pubtator3 --variant "$v" --pmid-from-external-id || log "pubtator3-$v: FAILED (rerun the same command to resume)"
  done
  finish_step
}

dispatch "$@"
