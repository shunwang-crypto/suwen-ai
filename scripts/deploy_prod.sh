#!/usr/bin/env bash
set -euo pipefail

# Idempotent single-host release procedure. Secrets stay in the server-only
# .env.production file and are never passed as command-line arguments.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.production}"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE; copy .env.production.example and fill every secret"
command -v docker >/dev/null 2>&1 || die "docker is required"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"

required=(
  APP_ENV EDUAGENTS_DOMAIN WEB_ORIGIN POSTGRES_DB POSTGRES_MIGRATION_PASSWORD
  POSTGRES_APP_PASSWORD REDIS_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD JWT_SECRET
  CHEAP_API_KEY STANDARD_API_KEY STRONG_API_KEY EMBEDDING_API_KEY SMTP_HOST SMTP_USER
  SMTP_PASSWORD SMTP_FROM
)
for name in "${required[@]}"; do
  value="$(grep -E "^${name}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  [[ -n "$value" ]] || die "$name is missing from $ENV_FILE"
  [[ "$value" != REPLACE_* ]] || die "$name still has a REPLACE_* placeholder"
done

grep -q '^APP_ENV=prod$' "$ENV_FILE" || die "APP_ENV must be exactly prod"
grep -q '^GATEWAY_MODE=live$' "$ENV_FILE" || die "GATEWAY_MODE must be exactly live"
grep -q '^GATEWAY_REPLAY_FALLBACK_STUB=false$' "$ENV_FILE" || die "stub fallback must be false"
grep -q '^WEB_IMPORT_ALLOW_PRIVATE_HOSTS=false$' "$ENV_FILE" || die "private URL import must be false"
grep -q '^ENABLE_TIME_TRAVEL=0$' "$ENV_FILE" || die "time travel must be disabled"
grep -q '^PAYMENT_PROVIDER=disabled$' "$ENV_FILE" || die "mock payment must be disabled"
grep -q '^ENABLE_FAULT_INJECTION=0$' "$ENV_FILE" || die "fault injection must be disabled"
grep -q '^LANGFUSE_LOG_CONTENT=0$' "$ENV_FILE" || die "model/user content logging must be disabled"

image_tag="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
compose=(env "PRODUCTION_ENV_FILE=$ENV_FILE" "EDUAGENTS_IMAGE_TAG=$image_tag" docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
"${compose[@]}" config >/dev/null
echo "[deploy] validated production compose and secret policy"

"${compose[@]}" build --pull api worker web
"${compose[@]}" up -d postgres redis qdrant minio
"${compose[@]}" up -d --wait postgres redis qdrant minio
echo "[deploy] infrastructure is healthy; running one migration job"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d api worker web caddy
"${compose[@]}" up -d --wait api worker web caddy

"${compose[@]}" ps
echo "[deploy] local container checks passed; run scripts/verify_prod.sh from another network"
