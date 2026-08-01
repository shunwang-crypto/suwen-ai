#!/usr/bin/env bash
set -euo pipefail

# Local production rehearsal. It never calls a model or SMTP. Use --empty-volumes
# only for a disposable rehearsal project; it removes this Compose project's volumes.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.production}"
BASE_URL="${BASE_URL:-https://localhost}"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"

die() { echo "[verify-prod-local] ERROR: $*" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE"
command -v docker >/dev/null 2>&1 || die "docker is required"

compose=(env "PRODUCTION_ENV_FILE=$ENV_FILE" docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
if [[ "${1:-}" == "--empty-volumes" ]]; then
  "${compose[@]}" down --volumes --remove-orphans
fi

"${compose[@]}" config >/dev/null
"${compose[@]}" up -d --build postgres redis qdrant minio
"${compose[@]}" up -d --wait postgres redis qdrant minio
"${compose[@]}" up --no-deps minio-init
init_status=$("${compose[@]}" ps -a --format json minio-init | python -c 'import json,sys; row=json.load(sys.stdin); print(row.get("State",""), row.get("ExitCode",""))')
[[ "$init_status" == "exited 0" ]] || die "MinIO initialization failed: $init_status"
"${compose[@]}" run --rm --no-deps migrate
migration_head=$("${compose[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"')
[[ "$migration_head" == "0013" ]] || die "unexpected migration head: $migration_head"
"${compose[@]}" up -d api worker web caddy
"${compose[@]}" up -d --wait api worker web caddy

for service in postgres redis qdrant minio api worker web caddy; do
  state=$("${compose[@]}" ps --format json "$service" | python -c 'import json,sys; print(json.load(sys.stdin).get("Health",""))')
  [[ "$state" == "healthy" ]] || die "$service is not healthy: $state"
done

for service in postgres redis qdrant minio; do
  container=$("${compose[@]}" ps -q "$service")
  ! docker inspect "$container" --format '{{json .NetworkSettings.Ports}}' | grep -q 'HostPort'
done

curl_args=(--silent --show-error --fail --connect-timeout 5 --max-time 20)
curl "${curl_args[@]}" -k "$BASE_URL/" >/dev/null
curl "${curl_args[@]}" -k "$BASE_URL/api/health/ready" >/dev/null
[[ "$(curl --silent --show-error --connect-timeout 5 --max-time 20 -k -o /dev/null -w '%{http_code}' "$BASE_URL/api/docs")" == 404 ]]
[[ "$(curl --silent --show-error --connect-timeout 5 --max-time 20 -k -o /dev/null -w '%{http_code}' "$BASE_URL/api/openapi.json")" == 404 ]]

for service in api worker; do
  "${compose[@]}" exec -T "$service" python - <<'PY'
import socket
socket.create_connection(("1.1.1.1", 443), timeout=5).close()
PY
done

for service in api worker; do
  env_output=$("${compose[@]}" exec -T "$service" env)
  ! grep -q '^POSTGRES_MIGRATION_PASSWORD=.' <<<"$env_output"
  ! grep -q '^MIGRATION_DATABASE_URL=.' <<<"$env_output"
  ! grep -q '^MINIO_ROOT_USER=.' <<<"$env_output"
  ! grep -q '^MINIO_ROOT_PASSWORD=.' <<<"$env_output"
  grep -q '^MINIO_APP_ACCESS_KEY=.' <<<"$env_output"
  grep -q '^MINIO_APP_SECRET_KEY=.' <<<"$env_output"
done

"${compose[@]}" exec -T api python - <<'PY'
import io
import os

from minio import Minio
client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_APP_ACCESS_KEY"],
    secret_key=os.environ["MINIO_APP_SECRET_KEY"],
    secure=False,
)
bucket = os.environ["MINIO_BUCKET"]
key = "__local_rehearsal__/rw-check"
client.put_object(bucket, key, io.BytesIO(b"ok"), 2)
response = client.get_object(bucket, key)
try:
    assert response.read() == b"ok"
finally:
    response.close()
    response.release_conn()
client.remove_object(bucket, key)
PY

if "${compose[@]}" run --rm --no-deps --entrypoint /bin/sh minio-init -c \
  'mc alias set app http://minio:9000 "$MINIO_APP_ACCESS_KEY" "$MINIO_APP_SECRET_KEY" >/dev/null && mc admin info app >/dev/null 2>&1'; then
  die "application user unexpectedly has admin access"
fi

echo "local production rehearsal: PASS"
