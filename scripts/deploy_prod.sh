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

if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no)" ]]; then
  die "tracked files are modified or staged; deploy only from a clean checked-out commit"
fi

commit="$(git -C "$ROOT_DIR" rev-parse HEAD)"
image_tag="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"

required=(
  APP_ENV EDUAGENTS_DOMAIN WEB_ORIGIN REGISTRATION_MODE POSTGRES_DB POSTGRES_MIGRATION_PASSWORD
  POSTGRES_APP_PASSWORD REDIS_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD
  MINIO_APP_ACCESS_KEY MINIO_APP_SECRET_KEY JWT_SECRET
  CHEAP_API_KEY CHEAP_BASE_URL CHEAP_MODEL STANDARD_API_KEY STANDARD_BASE_URL STANDARD_MODEL
  STRONG_API_KEY STRONG_BASE_URL STRONG_MODEL EMBEDDING_API_KEY EMBEDDING_BASE_URL
  EMBEDDING_MODEL RERANK_MODEL SMTP_HOST SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_REPLY_TO
)
for name in "${required[@]}"; do
  value="$(grep -E "^${name}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  [[ -n "$value" ]] || die "$name is missing from $ENV_FILE"
  [[ "$value" != REPLACE_* ]] || die "$name still has a REPLACE_* placeholder"
  [[ "$value" != *fake* && "$value" != *stub* && "$value" != *rehearsal* ]] || die "$name contains a fake/stub/rehearsal value"
done

grep -q '^APP_ENV=prod$' "$ENV_FILE" || die "APP_ENV must be exactly prod"
registration_mode="$(grep -E '^REGISTRATION_MODE=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
case "$registration_mode" in
  closed|invite|open) ;;
  *) die "REGISTRATION_MODE must be closed, invite, or open" ;;
esac
if grep -q '^PUBLIC_REGISTRATION=' "$ENV_FILE"; then
  die "PUBLIC_REGISTRATION is obsolete; remove it and use REGISTRATION_MODE only"
fi
grep -q '^GATEWAY_MODE=live$' "$ENV_FILE" || die "GATEWAY_MODE must be exactly live"
grep -q '^GATEWAY_REPLAY_FALLBACK_STUB=false$' "$ENV_FILE" || die "stub fallback must be false"
grep -q '^EMAIL_VERIFICATION_REQUIRED=true$' "$ENV_FILE" || die "email verification must be required"
grep -q '^EMBEDDING_MODE=live$' "$ENV_FILE" || die "embedding mode must be live"
grep -q '^RERANK_MODE=live$' "$ENV_FILE" || die "rerank mode must be live"
grep -q '^WEB_IMPORT_ALLOW_PRIVATE_HOSTS=false$' "$ENV_FILE" || die "private URL import must be false"
grep -q '^ENABLE_TIME_TRAVEL=0$' "$ENV_FILE" || die "time travel must be disabled"
grep -q '^PAYMENT_PROVIDER=disabled$' "$ENV_FILE" || die "mock payment must be disabled"
grep -q '^ENABLE_FAULT_INJECTION=0$' "$ENV_FILE" || die "fault injection must be disabled"
grep -q '^LANGFUSE_LOG_CONTENT=0$' "$ENV_FILE" || die "model/user content logging must be disabled"

compose=(env "PRODUCTION_ENV_FILE=$ENV_FILE" "EDUAGENTS_IMAGE_TAG=$image_tag" docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
"${compose[@]}" config >/dev/null
echo "[deploy] commit=$commit image_tag=eduagents-api:$image_tag image_tag=eduagents-web:$image_tag"
echo "[deploy] validated production compose and secret policy"

"${compose[@]}" build --pull api worker web
"${compose[@]}" up -d postgres redis qdrant minio
"${compose[@]}" up -d --wait postgres redis qdrant minio
"${compose[@]}" up --no-deps minio-init
init_status=$("${compose[@]}" ps -a --format json minio-init | python -c 'import json,sys; row=json.load(sys.stdin); print(row.get("State",""), row.get("ExitCode",""))')
[[ "$init_status" == "exited 0" ]] || die "MinIO initialization failed: $init_status"
echo "[deploy] infrastructure is healthy; running one migration job"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d api worker web caddy
"${compose[@]}" up -d --wait api worker web caddy

"${compose[@]}" ps
echo "[deploy] local container checks passed; run scripts/verify_prod.sh from another network"
