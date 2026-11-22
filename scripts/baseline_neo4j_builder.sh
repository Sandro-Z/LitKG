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
# No Hugging Face access? Add one of: --proxy http://host:port (image build only; LLM calls never use it),
# --hf-endpoint https://<HF mirror>, or --embedding-model-dir <local all-MiniLM-L6-v2 copy>.
# Needs Docker Compose. Run it as your normal user (not "sudo scripts/..."); it uses sudo for docker
# by itself when needed. Ports bind to 127.0.0.1 only (Neo4j 17687, builder 18000).
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
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
source "$ROOT/baselines/lib.sh"
DOCKER=(docker)
pick_docker() {   # plain docker if this user may use it, otherwise sudo docker (asks in the foreground)
  if docker info >/dev/null 2>&1; then DOCKER=(docker)
  elif command -v sudo >/dev/null && sudo docker info >/dev/null; then DOCKER=(sudo --preserve-env=KGB_SECRETS,KGB_BUILDER_DIR,KGB_NEO4J_BOLT_PORT,KGB_BUILDER_PORT,KGB_BUILD_PROXY,KGB_HF_ENDPOINT docker)
  else die "cannot run docker (not installed, or no permission and sudo failed)"; fi
  "${DOCKER[@]}" compose version >/dev/null 2>&1 || die "the Docker Compose plugin is required (docker compose version)"
}
compose() { "${DOCKER[@]}" compose -f "$COMPOSE_FILE" "$@"; }

prepare_config() {
  command -v docker >/dev/null || die "Docker is required for the Neo4j LLM Graph Builder"
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

# ---- image build options (no Hugging Face access on many production hosts)
BUILD_OPTIONS="$WORK/build_options.env"   # may contain proxy credentials: mode 600, never printed

save_build_options() {   # $1 proxy  $2 hf endpoint  $3 local model dir; empty keeps the saved value
  mkdir -p "$WORK"
  local proxy="" hf="" model=""
  [[ -f "$BUILD_OPTIONS" ]] && source "$BUILD_OPTIONS"
  proxy="${1:-${KGB_BUILD_PROXY:-}}"; hf="${2:-${KGB_HF_ENDPOINT:-}}"; model="${3:-${KGB_EMBEDDING_MODEL_DIR:-}}"
  (umask 077; printf 'KGB_BUILD_PROXY=%q\nKGB_HF_ENDPOINT=%q\nKGB_EMBEDDING_MODEL_DIR=%q\n' "$proxy" "$hf" "$model" > "$BUILD_OPTIONS")
}

load_build_options() {
  KGB_BUILD_PROXY=""; KGB_HF_ENDPOINT=""; KGB_EMBEDDING_MODEL_DIR=""
  [[ -f "$BUILD_OPTIONS" ]] && source "$BUILD_OPTIONS"
  export KGB_BUILD_PROXY KGB_HF_ENDPOINT KGB_EMBEDDING_MODEL_DIR
}

make_dockerfile() {
  # Upstream Dockerfile with only these changes:
  #  * embedding model: copied from --embedding-model-dir, or downloaded via HF_ENDPOINT (mirror) and/or proxy
  #  * NLTK data (used only by chat evaluation / GCS sources): best effort, never fails the build
  local backend="$BUILDER_DIR/backend"
  if [[ -n "$KGB_EMBEDDING_MODEL_DIR" ]]; then
    [[ -f "$KGB_EMBEDDING_MODEL_DIR/config.json" ]] || { log "--embedding-model-dir has no config.json: $KGB_EMBEDDING_MODEL_DIR"; return 1; }
    compgen -G "$KGB_EMBEDDING_MODEL_DIR/*.safetensors" >/dev/null || [[ -f "$KGB_EMBEDDING_MODEL_DIR/pytorch_model.bin" ]] \
      || { log "--embedding-model-dir has no model weights (*.safetensors or pytorch_model.bin)"; return 1; }
    mkdir -p "$backend/local_model"
    cp -a "$KGB_EMBEDDING_MODEL_DIR"/. "$backend/local_model/"
    log "using the pre-downloaded embedding model from $KGB_EMBEDDING_MODEL_DIR"
  fi
  "$VENV/bin/python" - "$backend/Dockerfile" "$backend/Dockerfile.kgb" "${KGB_EMBEDDING_MODEL_DIR:+local}" <<'PY'
import sys
source, target, mode = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(source).read().split('\n')
out, i, replaced = [], 0, False
while i < len(lines):
    line = lines[i]
    block = [line]
    while block[-1].rstrip().endswith('\\') and i + 1 < len(lines):
        i += 1
        block.append(lines[i])
    text = '\n'.join(block)
    if text.startswith('RUN') and 'AutoTokenizer' in text:
        replaced = True
        if mode == 'local':
            out.append('# kgb: embedding model supplied in ./local_model (copied in by "COPY . /code"); no download')
        else:
            out += ['# kgb: HF_ENDPOINT lets the download use a mirror; proxy build args apply too', 'ARG HF_ENDPOINT', text]
    elif text.startswith('RUN python -m nltk.downloader'):
        out.append(text + ' || echo "kgb: NLTK data not downloaded (only needed for chat evaluation / GCS sources)"')
    else:
        out.append(text)
    i += 1
if not replaced:
    sys.exit('upstream Dockerfile changed: embedding download step not found')
open(target, 'w').write('\n'.join(out))
PY
}

builder_ready() { neo4j_ready && curl -sf -o /dev/null "http://127.0.0.1:$KGB_BUILDER_PORT/docs"; }

start_builder() {
  if [[ ! -d "$BUILDER_DIR/.git" ]]; then
    log "cloning $BUILDER_REPO@$BUILDER_REF"
    load_build_options
    HTTPS_PROXY="${KGB_BUILD_PROXY:-${HTTPS_PROXY:-}}" git clone -q --depth 1 --branch "$BUILDER_REF" "$BUILDER_REPO" "$BUILDER_DIR"
  fi
  [[ "$(git -C "$BUILDER_DIR" rev-parse HEAD)" == "$BUILDER_COMMIT" ]] || { log "builder checkout is not the pinned commit"; return 1; }
  builder_secrets
  load_build_options
  make_dockerfile || return 1
  [[ -n "$KGB_BUILD_PROXY" ]] && log "image build uses the configured HTTP proxy"
  [[ -n "$KGB_HF_ENDPOINT" ]] && log "embedding model download uses HF_ENDPOINT=$KGB_HF_ENDPOINT"
  log "building/starting Neo4j + builder backend (first build downloads several GB)"
  compose up -d --build
  local i
  for i in $(seq 1 120); do
    if builder_ready; then log "builder ready"; return 0; fi
    sleep 10
  done
  log "builder not ready after 20 minutes; see: docker compose -f $COMPOSE_FILE logs"; return 1
}

before_background() {   # foreground: docker may need sudo, which can only prompt here
  if [[ -s "$WORK/out/graph.neo4j-builder-pdfnative.json" && "$FORCE" != 1 ]]; then return 0; fi
  pick_docker
  start_builder || die "the builder did not start; fix the error above and rerun the same command"
}

worker() {
  local force="$1"
  log "Neo4j LLM Graph Builder baseline started"
  corpus_step --no-pmid
  if [[ -s "$WORK/out/graph.neo4j-builder-pdfnative.json" && "$force" != 1 ]]; then
    log "graph already generated (use --force to rebuild from checkpoints)"
  else
    builder_ready || { log "FAILED: Neo4j/builder not reachable; rerun the same command to restart them"; exit 1; }
    log "uploading PDFs and extracting (checkpointed per PDF)"
    kgb run neo4j --variant pdfnative || log "FAILED (rerun the same command to resume)"
  fi
  finish_step
}

if [[ "${1:-}" == down ]]; then pick_docker; compose down; exit 0; fi
# Build-only options (consumed here; saved for later reruns): --proxy, --hf-endpoint, --embedding-model-dir
ARGS=(); OPT_PROXY=""; OPT_HF=""; OPT_MODEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy) OPT_PROXY="$2"; shift 2 ;;
    --hf-endpoint) OPT_HF="$2"; shift 2 ;;
    --embedding-model-dir) OPT_MODEL="$(cd "$2" && pwd)"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
case "${ARGS[0]:-}" in status|logs|stop|_worker|""|-h|--help) ;; *) save_build_options "$OPT_PROXY" "$OPT_HF" "$OPT_MODEL" ;; esac
dispatch "${ARGS[@]}"
