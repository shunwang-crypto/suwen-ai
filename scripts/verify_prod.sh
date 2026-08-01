#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${PROD_URL:-}}"
[[ -n "$BASE_URL" ]] || { echo "usage: $0 https://your-domain" >&2; exit 2; }
[[ "$BASE_URL" == https://* ]] || { echo "BASE_URL must use https://" >&2; exit 2; }
BASE_URL="${BASE_URL%/}"
host="${BASE_URL#https://}"
host="${host%%/*}"
[[ "$host" != localhost && "$host" != 127.* && "$host" != 10.* && "$host" != 192.168.* ]] || {
  echo "refusing a localhost/private URL: external verification must use the public hostname" >&2
  exit 2
}

tmp_dir="$(mktemp -d /tmp/eduagents-verify.XXXXXX)"
live_file="$tmp_dir/live.json"
ready_file="$tmp_dir/ready.json"

curl -fsS --max-time 20 "$BASE_URL/api/health/live" -o "$live_file"
curl -fsS --max-time 20 "$BASE_URL/api/health/ready" -o "$ready_file"
curl -fsS --max-time 20 "$BASE_URL/" -o /dev/null

for path in /api/docs /api/openapi.json; do
  status="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "$BASE_URL$path")"
  [[ "$status" == 404 ]] || { echo "$path must return 404, got $status" >&2; exit 1; }
done
echo "HTTPS page and private API docs: PASS"

python - "$live_file" "$ready_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    live = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    ready = json.load(handle)
if live.get("status") != "ok":
    raise SystemExit(f"live probe failed: {live!r}")
checks = ready.get("checks", {})
if ready.get("status") != "ok" or any(checks.get(k) != "ok" for k in ("postgres", "redis", "qdrant", "worker")):
    raise SystemExit(f"readiness probe failed: {ready!r}")
print("HTTPS live/readiness: PASS")
print("readiness:", json.dumps(ready, ensure_ascii=False, sort_keys=True))
PY

redirect="$(curl -sS --max-time 15 -o /dev/null -D - -w '%{http_code} %{redirect_url}' "http://$host/" | tail -n 1)"
case "$redirect" in
  301\ https://*|308\ https://*) echo "HTTP to HTTPS redirect: PASS ($redirect)" ;;
  *) echo "HTTP to HTTPS redirect: FAIL ($redirect)" >&2; exit 1 ;;
esac

openssl s_client -connect "$host:443" -servername "$host" </dev/null 2>/dev/null \
  | openssl x509 -checkhost "$host" -noout -subject -issuer -dates
echo "TLS certificate: PASS"

if command -v nc >/dev/null 2>&1; then
  for port in 5432 6379 6333 9000 9001; do
    if nc -z -w 3 "$host" "$port" >/dev/null 2>&1; then
      echo "unexpected public port open: $port" >&2
      exit 1
    fi
    echo "public port $port: closed"
  done
fi

echo "external verification complete: $BASE_URL"
