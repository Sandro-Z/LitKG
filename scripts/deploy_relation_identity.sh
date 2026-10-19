#!/usr/bin/env bash
# Deploy the batch-23 relation identity/display fix after source regression tests.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$root"
if [ "$(id -u)" -eq 0 ]; then
    docker_cmd=(docker)
else
    sudo -v
    docker_cmd=(sudo -n docker)
fi
project=$("${docker_cmd[@]}" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' litkg-api)
test -n "$project"
compose=("${docker_cmd[@]}" compose -p "$project")
# Existing extraction jobs must finish before replacing worker processes.
"${docker_cmd[@]}" exec -i litkg-api python - <<'PY'
from app.db import get_conn
from app.recovery import ACTIVE
with get_conn() as conn:
    active = conn.execute('SELECT id,status FROM documents WHERE status=ANY(%s)', (ACTIVE,)).fetchall()
    assert not active, f'Wait for active documents to finish before deployment: {active}'
PY
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
backup="$root/../litkg-backups/relation-deployment-$stamp"
mkdir -m 700 -- "$backup"
for service in api worker_parse worker_extract frontend; do
    container=$("${compose[@]}" ps -q "$service")
    image=$("${docker_cmd[@]}" inspect -f '{{.Image}}' "$container")
    tag="$project-$service:before-relation-$stamp"
    "${docker_cmd[@]}" image tag "$image" "$tag"
    printf '%s %s %s\n' "$service" "$image" "$tag" >> "$backup/images.txt"
done
"${compose[@]}" build api worker_parse worker_extract frontend
"${compose[@]}" up -d --no-deps --force-recreate --timeout 330 api worker_parse worker_extract frontend
expected=$(sha256sum app/app/facts.py | cut -d' ' -f1)
for service in api worker_parse worker_extract; do
    container=$("${compose[@]}" ps -q "$service")
    printf '%s  /app/app/facts.py\n' "$expected" | "${docker_cmd[@]}" exec -i "$container" sha256sum -c -
done
expected=$(sha256sum frontend/app.py | cut -d' ' -f1)
printf '%s  /frontend/app.py\n' "$expected" | "${docker_cmd[@]}" exec -i litkg-frontend sha256sum -c -
python3 - <<'PY'
import json, time, urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3) as response:
            health = json.load(response)
        assert health.get('status') == 'ok', health
        print('API health:', health)
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(2)
PY
"${compose[@]}" ps api worker_parse worker_extract frontend
printf 'Deployed. Rollback image references: %s\n' "$backup/images.txt"
