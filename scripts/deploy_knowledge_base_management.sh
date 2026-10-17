#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$root"
if [ "$(id -u)" -eq 0 ]; then
    docker_cmd=(docker)
else
    sudo -v
    docker_cmd=(sudo -n docker)
fi
sha256sum -c scripts/knowledge-base-runtime.sha256
project=$("${docker_cmd[@]}" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' litkg-api)
test -n "$project"
compose=("${docker_cmd[@]}" compose -p "$project")
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
backup="$root/.deploy-backups/knowledge-base-management-$stamp"
mkdir -p -- "$backup"
for service in api worker_parse worker_extract frontend; do
    container=$("${compose[@]}" ps -q "$service")
    old_image=$("${docker_cmd[@]}" inspect -f '{{.Image}}' "$container")
    tag="$project-$service:before-kb-management-$stamp"
    "${docker_cmd[@]}" image tag "$old_image" "$tag"
    printf '%s %s %s\n' "$service" "$old_image" "$tag" >> "$backup/images.txt"
done
"${compose[@]}" exec -T postgres sh -ec 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" -Fc' > "$backup/database.dump"
printf 'Rollback images and database backup: %s\n' "$backup"
"${compose[@]}" build api worker_parse worker_extract frontend
image="$project-api:latest"
"${docker_cmd[@]}" run --rm -i -w / "$image" sha256sum -c - < scripts/knowledge-base-api.sha256
"${docker_cmd[@]}" run --rm -i --entrypoint sha256sum "$project-frontend:latest" -c - < scripts/knowledge-base-frontend.sha256

network="litkg-kb-check-$stamp"
postgres="litkg-kb-pg-$stamp"
test_container="litkg-kb-tests-$stamp"
ui_container="litkg-kb-ui-tests-$stamp"
cleanup() {
    "${docker_cmd[@]}" rm -f "$test_container" "$ui_container" "$postgres" >/dev/null 2>&1 || true
    "${docker_cmd[@]}" network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
"${docker_cmd[@]}" network create "$network" >/dev/null
"${docker_cmd[@]}" run -d --rm --name "$postgres" --network "$network" --network-alias test-postgres \
    --tmpfs /var/lib/postgresql/data:rw,size=512m \
    -e POSTGRES_USER=litkg_test -e POSTGRES_PASSWORD=litkg_test_only -e POSTGRES_DB=litkg_test \
    pgvector/pgvector:pg16 >/dev/null
ready=false
for attempt in {1..30}; do
    if "${docker_cmd[@]}" exec "$postgres" pg_isready -U litkg_test -d litkg_test >/dev/null 2>&1; then ready=true; break; fi
    sleep 1
done
if ! $ready; then printf 'Disposable test PostgreSQL did not become ready.\n' >&2; exit 1; fi
"${docker_cmd[@]}" run --rm --name "$test_container" --network "$network" \
    -v "$root/tests:/verification/tests:ro" -v "$root/db:/verification/db:ro" \
    -w /verification -e PYTHONPATH=/app:/verification/tests -e PYTHONDONTWRITEBYTECODE=1 \
    -e LITKG_TEST_DATABASE_URL=postgresql://litkg_test:litkg_test_only@test-postgres:5432/litkg_test \
    "$image" sh -ec 'python -m pip install --disable-pip-version-check --quiet httpx; python -m unittest discover -s /verification/tests -q'
"${docker_cmd[@]}" run --rm --name "$ui_container" \
    -v "$root/tests:/verification/tests:ro" -v "$root/frontend:/verification/frontend:ro" \
    -v "$root/app/app:/verification/app:ro" -w /verification \
    -e PYTHONPATH=/verification:/verification/tests -e PYTHONDONTWRITEBYTECODE=1 \
    "$project-frontend:latest" python -m unittest test_frontend test_knowledge_base_frontend -q
cleanup
trap - EXIT

printf 'Regression tests passed. Updating API, workers and frontend.\n'
"${docker_cmd[@]}" exec -i litkg-postgres sh -ec 'exec psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -v ON_ERROR_STOP=1' \
    < db/migrations/20261017_processing_leases.sql
"${compose[@]}" up -d --no-deps --force-recreate --timeout 330 api worker_parse worker_extract frontend
for service in api worker_parse worker_extract; do
    container=$("${compose[@]}" ps -q "$service")
    "${docker_cmd[@]}" exec -i -w / "$container" sha256sum -c - < scripts/knowledge-base-api.sha256
done
container=$("${compose[@]}" ps -q frontend)
"${docker_cmd[@]}" exec -i "$container" sha256sum -c - < scripts/knowledge-base-frontend.sha256
python3 - <<'PY'
import json
import time
import urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3) as response:
            assert json.load(response)['status'] == 'ok'
        with urllib.request.urlopen('http://127.0.0.1:8080/openapi.json', timeout=3) as response:
            methods = json.load(response)['paths']['/knowledge-bases/{knowledge_base_id}']
            assert 'put' in methods and 'delete' in methods
        with urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3) as response:
            assert response.read().strip() == b'ok'
        print('API, management routes and frontend are healthy.')
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(2)
PY
"${compose[@]}" ps api worker_parse worker_extract frontend
printf 'Deployment verified. Rollback image references: %s\n' "$backup/images.txt"
