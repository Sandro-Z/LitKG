#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$root"
retry_failed=false
knowledge_base=19
documents=25,26,27,28,29
while [ "$#" -gt 0 ]; do
    case "$1" in
        --retry-failed) retry_failed=true; shift ;;
        --knowledge-base) knowledge_base="${2:?missing knowledge base ID}"; shift 2 ;;
        --documents) documents="${2:?missing comma-separated document IDs}"; shift 2 ;;
        *) printf 'Usage: bash %s [--retry-failed] [--knowledge-base ID] [--documents ID,ID,...]\n' "$0" >&2; exit 2 ;;
    esac
done
[[ "$knowledge_base" =~ ^[1-9][0-9]*$ && "$documents" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || exit 2
if [ "$(id -u)" -eq 0 ]; then
    docker_cmd=(docker)
else
    sudo -v
    docker_cmd=(sudo -n docker)
fi
sha256sum -c scripts/catalog-runtime.sha256
project=$("${docker_cmd[@]}" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' litkg-api)
test -n "$project"
compose=("${docker_cmd[@]}" compose -p "$project")
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
backup="$root/.deploy-backups/catalog-$stamp"
mkdir -p -- "$backup"
for service in api worker_parse worker_extract frontend; do
    container=$("${compose[@]}" ps -q "$service")
    old_image=$("${docker_cmd[@]}" inspect -f '{{.Image}}' "$container")
    rollback_tag="$project-$service:before-catalog-$stamp"
    "${docker_cmd[@]}" image tag "$old_image" "$rollback_tag"
    printf '%s %s %s\n' "$service" "$old_image" "$rollback_tag" >> "$backup/images.txt"
done
printf 'Building application images; rollback image references: %s\n' "$backup/images.txt"
"${compose[@]}" build api worker_parse worker_extract frontend
image="$project-api:latest"
"${docker_cmd[@]}" run --rm -i -w / "$image" sha256sum -c - < scripts/catalog-runtime.sha256

network="litkg-catalog-check-$stamp"
postgres="litkg-catalog-pg-$stamp"
test_container="litkg-catalog-tests-$stamp"
network_created=false
postgres_created=false
cleanup() {
    "${docker_cmd[@]}" rm -f "$test_container" >/dev/null 2>&1 || true
    if $postgres_created; then "${docker_cmd[@]}" rm -f "$postgres" >/dev/null 2>&1 || true; fi
    if $network_created; then "${docker_cmd[@]}" network rm "$network" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
"${docker_cmd[@]}" network create "$network" >/dev/null
network_created=true
"${docker_cmd[@]}" run -d --rm --name "$postgres" --network "$network" --network-alias test-postgres \
    --tmpfs /var/lib/postgresql/data:rw,size=512m \
    -e POSTGRES_USER=litkg_test -e POSTGRES_PASSWORD=litkg_test_only -e POSTGRES_DB=litkg_test \
    pgvector/pgvector:pg16 >/dev/null
postgres_created=true
ready=false
for attempt in {1..30}; do
    if "${docker_cmd[@]}" exec "$postgres" pg_isready -U litkg_test -d litkg_test >/dev/null 2>&1; then ready=true; break; fi
    sleep 1
done
if ! $ready; then printf 'Disposable test PostgreSQL did not become ready.\n' >&2; exit 1; fi
printf 'Running extraction/recovery and PostgreSQL regressions against disposable data.\n'
"${docker_cmd[@]}" run --rm --name "$test_container" --network "$network" \
    -v "$root/tests:/verification/tests:ro" -v "$root/db:/verification/db:ro" \
    -w /verification -e PYTHONPATH=/app:/verification/tests -e PYTHONDONTWRITEBYTECODE=1 \
    -e LITKG_TEST_DATABASE_URL=postgresql://litkg_test:litkg_test_only@test-postgres:5432/litkg_test \
    "$image" sh -ec 'python -m pip install --disable-pip-version-check --quiet httpx; python -m unittest test_api test_claim_robustness test_claims_robustness test_recovery test_catalog_robustness test_catalog_capacity test_knowledge_integration test_graph_integration test_entity_resolution_integration -q'
cleanup
trap - EXIT

printf 'Applying additive processing lease migration.\n'
"${docker_cmd[@]}" exec -i litkg-postgres sh -ec 'exec psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -v ON_ERROR_STOP=1' \
    < db/migrations/20261017_processing_leases.sql

printf 'Regression tests passed. Updating API, both workers and frontend.\n'
"${compose[@]}" up -d --no-deps --force-recreate --timeout 330 api worker_parse worker_extract frontend
for service in api worker_parse worker_extract; do
    container=$("${compose[@]}" ps -q "$service")
    "${docker_cmd[@]}" exec -i -w / "$container" sha256sum -c - < scripts/catalog-runtime.sha256
done
python3 - <<'PY'
import json,time,urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
            health=json.load(response)
        assert health.get('status')=='ok',health
        print('API health:',health)
        break
    except Exception:
        if attempt==29: raise
        time.sleep(2)
PY
for service in worker_parse worker_extract; do
    container=$("${compose[@]}" ps -q "$service")
    worker_host=$("${docker_cmd[@]}" inspect -f '{{.Config.Hostname}}' "$container")
    "${docker_cmd[@]}" exec "$container" celery -A app.celery_app inspect ping --destination "celery@$worker_host" --timeout 10
done
"${compose[@]}" ps api worker_parse worker_extract frontend
if $retry_failed; then
    python3 - "$knowledge_base" "$documents" <<'PY'
import json,sys,urllib.request
base='http://127.0.0.1:8080'
knowledge_base=int(sys.argv[1])
for document_id in map(int,sys.argv[2].split(',')):
    # The processing endpoint reconciles proven stale work before this check.
    with urllib.request.urlopen(f'{base}/knowledge-bases/{knowledge_base}/documents/{document_id}/processing',timeout=15) as response:
        document=json.load(response)['document']
    if document['status'] not in {'failed','partial'}:
        print('Skipped document',document_id,'with status',document['status'])
        continue
    request=urllib.request.Request(f'{base}/knowledge-bases/{knowledge_base}/maintenance',
        data=json.dumps({'mode':'retry','document_id':document_id}).encode(),
        headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(request,timeout=30) as response:
        job=json.load(response)
    print('Retry queued:',document_id,job['task_id'],job.get('status_url'))
PY
fi
printf 'Deployment verified. Rollback image references: %s\n' "$backup/images.txt"
