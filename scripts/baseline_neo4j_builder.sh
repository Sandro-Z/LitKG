#!/usr/bin/env bash
# Neo4j LLM Graph Builder baseline: PDFs + one LLM API -> graph in LitKG QC canonical JSON.
#
#   scripts/baseline_neo4j_builder.sh --pdfs <PDF目录> --llm-base-url <http://host:port/v1> --llm-model <模型>
#   scripts/baseline_neo4j_builder.sh status | logs | stop | down
#
# Clones neo4j-labs/llm-graph-builder v0.8.7, starts it with a dedicated Neo4j (Docker), uploads
# each PDF to the builder (its own PDF parser and LLM extraction, schema = LitKG entity types and
# predicates), and exports data/baselines/neo4j-builder/out/graph.neo4j-builder-pdfnative.json.
# Runs in the background; rerun the same command to resume (finished PDFs are checkpointed).
# Options: --name <语料名> --detach --force. "down" stops the Neo4j/builder containers.
# Needs Docker Compose; ports bind to 127.0.0.1 only (Neo4j 17687, builder 18000).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${KGB_WORK:-$ROOT/data/baselines/neo4j-builder}"
SYSTEM=neo4j_builder
BUILDER_DIR="$WORK/vendor/llm-graph-builder"
BUILDER_REPO="https://github.com/neo4j-labs/llm-graph-builder"
BUILDER_REF="v0.8.7"
BUILDER_COMMIT="1ade47de63edb161e5ea37a277141643f3af3142"
COMPOSE_FILE="$ROOT/baselines/deploy/docker-compose.builder.yml"
export KGB_SECRETS="$WORK/secrets" KGB_BUILDER_DIR="$BUILDER_DIR"
export KGB_NEO4J_BOLT_PORT="${KGB_NEO4J_BOLT_PORT:-17687}" KGB_BUILDER_PORT="${KGB_BUILDER_PORT:-18000}"
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
source "$ROOT/baselines/lib.sh"
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

prepare_config() {
  command -v docker >/dev/null && docker compose version >/dev/null 2>&1 || die "Docker Compose is required"
  write_config "$BASE_URL" "$MODEL" "$NAME" "
[neo4j]
uri = \"bolt://127.0.0.1:$KGB_NEO4J_BOLT_PORT\"
backend_uri = \"bolt://neo4j:7687\"
backend_url = \"http://127.0.0.1:$KGB_BUILDER_PORT\"
password_file = \"$SECRETS/neo4j_password\"
model = \"litkg_compat\"
model_name = \"$MODEL\"
builder_ref = \"$BUILDER_REF\"
builder_commit = \"$BUILDER_COMMIT\"
dedicated_database = true"
  printf '%s\n' "$BASE_URL" "$MODEL" > "$WORK/llm_target"
}

builder_secrets() {   # the builder reads model,url,key from one env value; written mode 600
  local base_url model
  { read -r base_url; read -r model; } < "$WORK/llm_target"
  base_url="${base_url/:\/\/localhost/://host.docker.internal}"
  base_url="${base_url/:\/\/127.0.0.1/://host.docker.internal}"
  grep -q ',' "$SECRETS/llm_api_key" && { log "the builder's env format cannot hold an API key containing commas"; return 1; }
  (umask 077
   [[ -s "$SECRETS/neo4j_password" ]] || "$VENV/bin/python" -c 'import secrets;print(secrets.token_hex(16),end="")' > "$SECRETS/neo4j_password"
   printf 'NEO4J_AUTH=neo4j/%s\n' "$(cat "$SECRETS/neo4j_password")" > "$SECRETS/neo4j.env"
   printf 'LLM_MODEL_CONFIG_LITKG_COMPAT=%s,%s,%s\nNEO4J_USERNAME=neo4j\nNEO4J_PASSWORD=%s\n' \
     "$model" "$base_url" "$(cat "$SECRETS/llm_api_key")" "$(cat "$SECRETS/neo4j_password")" > "$SECRETS/builder-backend.env")
}

neo4j_ready() {
  "$VENV/bin/python" - "$KGB_NEO4J_BOLT_PORT" "$SECRETS/neo4j_password" 2>/dev/null <<'PY'
import sys
from neo4j import GraphDatabase
with GraphDatabase.driver(f"bolt://127.0.0.1:{sys.argv[1]}", auth=("neo4j", open(sys.argv[2]).read().strip())) as d:
    d.verify_connectivity()
PY
}

start_builder() {
  if [[ ! -d "$BUILDER_DIR/.git" ]]; then
    log "cloning $BUILDER_REPO@$BUILDER_REF"
    git clone -q --depth 1 --branch "$BUILDER_REF" "$BUILDER_REPO" "$BUILDER_DIR"
  fi
  [[ "$(git -C "$BUILDER_DIR" rev-parse HEAD)" == "$BUILDER_COMMIT" ]] || { log "builder checkout is not the pinned commit"; return 1; }
  builder_secrets
  log "building/starting Neo4j + builder backend (first build downloads several GB)"
  compose up -d --build
  local i
  for i in $(seq 1 120); do
    if neo4j_ready && curl -sf -o /dev/null "http://127.0.0.1:$KGB_BUILDER_PORT/docs"; then log "builder ready"; return 0; fi
    sleep 10
  done
  log "builder not ready after 20 minutes; see: docker compose -f $COMPOSE_FILE logs"; return 1
}

worker() {
  local force="$1"
  log "Neo4j LLM Graph Builder baseline started"
  corpus_step --no-pmid
  if [[ -s "$WORK/out/graph.neo4j-builder-pdfnative.json" && "$force" != 1 ]]; then
    log "graph already generated (use --force to rebuild from checkpoints)"
  else
    start_builder || { log "FAILED to start the builder"; exit 1; }
    log "uploading PDFs and extracting (checkpointed per PDF)"
    kgb run neo4j --variant pdfnative || log "FAILED (rerun the same command to resume)"
  fi
  finish_step
}

if [[ "${1:-}" == down ]]; then compose down; exit 0; fi
dispatch "$@"
