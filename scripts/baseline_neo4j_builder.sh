#!/usr/bin/env bash
# Neo4j LLM Graph Builder baseline: PDFs + one LLM API -> graph in LitKG QC canonical JSON.
#
#   scripts/baseline_neo4j_builder.sh --pdfs <PDF目录> --llm-base-url <http://host:port/v1> --llm-model <模型>
#   scripts/baseline_neo4j_builder.sh status | logs | stop | down | docker-logs
#
# Clones neo4j-labs/llm-graph-builder v0.8.7, starts it with a dedicated Neo4j (Docker), uploads
# each PDF to the builder (its own PDF parser and LLM extraction, schema = LitKG entity types and
# predicates), and exports data/baselines/neo4j-builder/out/graph.neo4j-builder-pdfnative.json.
# Runs in the background; rerun the same command to resume (finished PDFs are checkpointed).
# Options: --name <语料名> --detach --force. "down" stops the Neo4j/builder containers;
# "docker-logs" shows their logs. Needs >= KGB_MIN_FREE_GB (default 5) GB free for Docker's data.
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
MODEL_KEY=litkg_compat   # the builder reads LLM_MODEL_CONFIG_LITKG_COMPAT=model,url,key (generic OpenAI-compatible branch)
GRAPH="$WORK/out/graph.neo4j-builder-pdfnative.json"
FAILED="$WORK/out/failed.neo4j-builder-pdfnative.json"   # written while some PDFs still fail
export KGB_SECRETS="$WORK/secrets" KGB_BUILDER_DIR="$BUILDER_DIR"
export KGB_NEO4J_BOLT_PORT="${KGB_NEO4J_BOLT_PORT:-17687}" KGB_BUILDER_PORT="${KGB_BUILDER_PORT:-18000}"
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
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
model = \"$MODEL_KEY\"
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
  local host
  host="${base_url#*://}"; host="${host%%[/:]*}"
  # The builder's OpenAI client (openai 2.x) refuses an empty key before sending any request; an LLM
  # server without authentication (vLLM/SGLang/Ollama without --api-key) ignores whatever key it gets.
  [[ -s "$SECRETS/llm_api_key" ]] || log "no LLM API key given: the builder gets the placeholder key EMPTY (fine for a server without authentication)"
  (umask 077
   key="$(cat "$SECRETS/llm_api_key")"
   [[ -s "$SECRETS/neo4j_password" ]] || "$VENV/bin/python" -c 'import secrets;print(secrets.token_hex(16),end="")' > "$SECRETS/neo4j_password"
   printf 'NEO4J_AUTH=neo4j/%s\n' "$(cat "$SECRETS/neo4j_password")" > "$SECRETS/neo4j.env"
   printf 'LLM_MODEL_CONFIG_%s=%s,%s,%s\nNEO4J_USERNAME=neo4j\nNEO4J_PASSWORD=%s\n' \
     "${MODEL_KEY^^}" "$model" "$base_url" "${key:-EMPTY}" "$(cat "$SECRETS/neo4j_password")" > "$SECRETS/builder-backend.env"
   # A proxy that Docker injects into containers (~/.docker/config.json) must not catch the LLM or Neo4j.
   printf 'NO_PROXY=%s,neo4j,localhost,127.0.0.1,host.docker.internal\nno_proxy=%s,neo4j,localhost,127.0.0.1,host.docker.internal\n' \
     "$host" "$host" >> "$SECRETS/builder-backend.env")
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
        # The builder splits text with tiktoken ("gpt2"), which otherwise downloads its BPE files from
        # openaipublic.blob.core.windows.net on first use, at extraction time and without the build proxy.
        out += ['# kgb: cache tiktoken files in the image so extraction needs no internet access',
                'ENV TIKTOKEN_CACHE_DIR=/opt/kgb/tiktoken',
                'RUN python -c "import tiktoken; names = [n for n in (\'gpt2\', \'cl100k_base\', \'o200k_base\') '
                'if n in tiktoken.list_encoding_names()]; [tiktoken.get_encoding(n) for n in names]; '
                'print(\'kgb: cached tiktoken encodings\', names)"']
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

disk_preflight() {   # Docker's data root holds the builder image (several GB) and the Neo4j volume
  local root avail
  root="$("${DOCKER[@]}" info -f '{{.DockerRootDir}}' 2>/dev/null)" || root=/var/lib/docker
  avail="$(df -Pk "$root" 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}')"
  [[ -n "$avail" ]] || return 0
  log "free disk space for Docker ($root): ${avail} GB"
  (( avail >= ${KGB_MIN_FREE_GB:-5} )) || { log "not enough free disk space for Docker ($root): ${avail} GB < ${KGB_MIN_FREE_GB:-5} GB.
  Neo4j stops working when the disk fills up. Free space first, e.g. check: df -h $root ; sudo docker system df
  (sudo docker builder prune / sudo docker image prune remove only build cache and dangling images)"; return 1; }
}

neo4j_problem() {   # $1 since: what Neo4j's own log shows that waiting will not fix
  local recent
  recent="$(compose logs --no-color --since "$1" neo4j 2>/dev/null | tail -n 2000)"
  if grep -q 'No space left on device' <<<"$recent"; then echo disk
  elif grep -q 'needs to be restarted' <<<"$recent"; then echo critical
  fi
}

start_builder() {
  if [[ ! -d "$BUILDER_DIR/.git" ]]; then
    log "cloning $BUILDER_REPO@$BUILDER_REF"
    load_build_options
    HTTPS_PROXY="${KGB_BUILD_PROXY:-${HTTPS_PROXY:-}}" git clone -q --depth 1 --branch "$BUILDER_REF" "$BUILDER_REPO" "$BUILDER_DIR"
  fi
  [[ "$(git -C "$BUILDER_DIR" rev-parse HEAD)" == "$BUILDER_COMMIT" ]] || { log "builder checkout is not the pinned commit"; return 1; }
  # LitKG's LLM request settings for the builder's OpenAI-compatible branch (baselines/deploy/patch_builder.py)
  git -C "$BUILDER_DIR" checkout -q -- backend/src/llm.py \
    && "$VENV/bin/python" "$ROOT/baselines/deploy/patch_builder.py" "$BUILDER_DIR/backend/src/llm.py" \
    || { log "cannot apply the LLM request settings to the builder"; return 1; }
  builder_secrets
  load_build_options
  make_dockerfile || return 1
  [[ -n "$KGB_BUILD_PROXY" ]] && log "image build uses the configured HTTP proxy"
  [[ -n "$KGB_HF_ENDPOINT" ]] && log "embedding model download uses HF_ENDPOINT=$KGB_HF_ENDPOINT"
  disk_preflight || return 1
  # A Neo4j left running by an earlier run may be stuck in a failed state (e.g. after the disk filled up):
  # Neo4j then refuses every transaction until it is restarted.
  if [[ -n "$(compose ps -q --status running neo4j 2>/dev/null)" ]] && ! neo4j_ready; then
    log "the running Neo4j does not accept connections; restarting it"
    compose restart neo4j
  fi
  save_builder_log
  log "building/starting Neo4j + builder backend (first build downloads several GB)"
  compose up -d --build || { log "docker compose could not build or start the builder (see the output above)"; return 1; }
  local i problem restarted=0 since
  since="$(date -u +%FT%TZ)"
  for i in $(seq 1 120); do
    if builder_ready; then log "builder ready"; return 0; fi
    problem="$(neo4j_problem "$since")"
    if [[ "$problem" == disk ]]; then
      log "Neo4j reports 'No space left on device'. Free disk space for Docker, then rerun the same command."
      disk_preflight || true; return 1
    elif [[ "$problem" == critical && "$restarted" == 0 ]]; then
      log "Neo4j reports a critical error; restarting it once"
      compose restart neo4j; restarted=1; since="$(date -u +%FT%TZ)"
    elif [[ "$problem" == critical ]]; then
      log "Neo4j keeps failing after a restart; see: $0 docker-logs"; return 1
    fi
    sleep 10
  done
  log "builder not ready after 20 minutes; see: $0 docker-logs"; return 1
}

builder_selftest() {   # in the container: the code paths /extract uses, with their real error messages
  local schema
  schema="$(kgb builder-schema --litkg-root "$ROOT")" || { log "cannot compute the LitKG schema"; return 1; }
  log "self-test inside the builder container (tokenizer, embedding model, Neo4j, one small LLM extraction)"
  { printf 'SCHEMA_B64 = "%s"\nMODEL_KEY = "%s"\n' "$(printf '%s' "$schema" | base64 -w0)" "$MODEL_KEY"
    cat "$ROOT/baselines/deploy/builder_selftest.py"; } | compose exec -T builder-backend python -
}

redact() {   # hide the LLM key and the Neo4j password (also base64, as sent to the builder) in shown text
  "$VENV/bin/python" -c '
import base64, sys
secrets = []
for path in sys.argv[1:]:
    try:
        value = open(path).read().strip()
    except OSError:
        continue
    if len(value) >= 4:
        secrets += [value, base64.b64encode(value.encode()).decode()]
for line in sys.stdin:
    for secret in secrets:
        line = line.replace(secret, "***")
    sys.stdout.write(line)
' "$SECRETS/llm_api_key" "$SECRETS/neo4j_password"
}

builder_traceback() {   # $1 since: the builder's own record of the first failed extraction (/extract hides it)
  local recent
  recent="$(compose logs --no-color --since "$1" builder-backend 2>/dev/null)" || return 1
  if grep -q 'File Failed in extraction' <<<"$recent"; then
    grep -m1 -A60 'File Failed in extraction' <<<"$recent" | redact
  else
    tail -n 30 <<<"$recent" | redact
  fi
}

save_builder_log() {   # recreating the container discards the builder's log: keep it and summarise failures
  [[ -n "$(compose ps -q builder-backend 2>/dev/null)" ]] || return 0
  local file="$WORK/logs/builder-backend.$(date -u +%Y%m%dT%H%M%SZ).log.gz" marker="$WORK/logs/.builder-log-saved" since=""
  [[ -s "$marker" ]] && since="$(cat "$marker")"
  compose logs --no-color ${since:+--since "$since"} builder-backend 2>/dev/null | redact | gzip > "$file" || true
  date -u +%FT%TZ > "$marker"
  if [[ -z "$(gzip -dc "$file" 2>/dev/null | head -c 1)" ]]; then rm -f "$file"; return 0; fi
  "$VENV/bin/python" "$ROOT/baselines/deploy/builder_log_summary.py" "$file" || true
  ls -1t "$WORK"/logs/builder-backend.*.log.gz 2>/dev/null | tail -n +6 | xargs -r rm -f   # keep the last 5
}

builder_smoke() {   # one small PDF through /upload and /extract: exactly the path every PDF takes
  local since
  since="$(date -u +%FT%TZ)"
  log "end-to-end test: one 2-sentence PDF through the builder's /upload and /extract (a minute or two)"
  kgb builder-smoke --litkg-root "$ROOT" && return 0
  log "the builder's own error log for this test (its API only says 'Failed To Process File'):"
  builder_traceback "$since" || log "(could not read it; see: $0 docker-logs)"
  return 1
}

docker_noninteractive() {   # background worker: docker without a password prompt, when possible
  if docker info >/dev/null 2>&1; then DOCKER=(docker)
  elif command -v sudo >/dev/null && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n --preserve-env=KGB_SECRETS,KGB_BUILDER_DIR,KGB_NEO4J_BOLT_PORT,KGB_BUILDER_PORT,KGB_BUILD_PROXY,KGB_HF_ENDPOINT docker)
  else return 1; fi
}

graph_complete() { [[ -s "$GRAPH" && ! -e "$FAILED" ]]; }

before_background() {   # foreground: docker may need sudo, which can only prompt here
  if graph_complete && [[ "$FORCE" != 1 ]]; then return 0; fi
  pick_docker
  start_builder || die "the builder did not start; fix the error above and rerun the same command"
  builder_selftest || die "the builder self-test failed (FAIL above); fix that and rerun the same command"
  builder_smoke || die "the end-to-end test failed (the builder's error is shown above); fix that and rerun the same command"
}

worker() {
  local force="$1"
  log "Neo4j LLM Graph Builder baseline started"
  corpus_step --no-pmid
  if graph_complete && [[ "$force" != 1 ]]; then
    log "graph already generated (use --force to rebuild from checkpoints)"
  else
    builder_ready || { log "FAILED: Neo4j/builder not reachable; rerun the same command to restart them"; exit 1; }
    [[ -e "$FAILED" ]] && log "retrying the PDFs that failed last time (finished PDFs come from checkpoints)"
    log "uploading PDFs and extracting (checkpointed per PDF)"
    local status=0 since
    since="$(date -u +%FT%TZ)"
    kgb run neo4j --variant pdfnative || status=$?
    if [[ "$status" != 0 ]] && docker_noninteractive; then
      log "the builder's own error log for the first failed PDF (its API only says 'Failed To Process File'):"
      builder_traceback "$since" || true
    fi
    if [[ "$status" == 3 ]]; then
      log "INCOMPLETE: some PDFs failed (listed in $FAILED). Cause: $0 docker-logs | grep -A40 'File Failed in extraction'"
      log "Rerun the same command to retry only those PDFs."
    elif [[ "$status" != 0 ]]; then
      log "FAILED (see the error above). Rerun the same command to resume; finished PDFs are checkpointed."
    fi
  fi
  finish_step
}

if [[ "${1:-}" == down ]]; then pick_docker; compose down; exit 0; fi
if [[ "${1:-}" == docker-logs ]]; then pick_docker; compose logs --no-color --tail "${2:-200}"; exit 0; fi
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
