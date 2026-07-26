#!/usr/bin/env bash
# 本地开发基础设施 (conda 版 PG/Redis, 见 docs/decisions.md D5)
# 幂等: 重复执行只会确保服务在运行
set -euo pipefail

ENV_BIN=/home/shunw/miniforge3/envs/eduagents/bin
DATA_ROOT="$HOME/.local/share/eduagents"
PGDATA="$DATA_ROOT/pgdata"
REDIS_DIR="$DATA_ROOT/redis"
mkdir -p "$DATA_ROOT" "$REDIS_DIR"

# ---------- PostgreSQL ----------
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "[dev-infra] initdb ..."
  "$ENV_BIN/initdb" -D "$PGDATA" -U edu --auth=trust -E UTF8 >/dev/null
fi
if ! "$ENV_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "[dev-infra] starting postgres ..."
  "$ENV_BIN/pg_ctl" -D "$PGDATA" -l "$DATA_ROOT/pg.log" -o "-p 5432" start >/dev/null
fi
if ! "$ENV_BIN/psql" -U edu -h 127.0.0.1 -p 5432 -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw eduagents; then
  echo "[dev-infra] creating database eduagents ..."
  "$ENV_BIN/createdb" -U edu -h 127.0.0.1 -p 5432 eduagents
fi

# ---------- Qdrant (musl 静态二进制服务端, 解除本地模式单进程限制 D11) ----------
QDRANT_BIN="$DATA_ROOT/qdrant-server/qdrant"
if [ -x "$QDRANT_BIN" ]; then
  if ! curl -s --max-time 2 http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
    echo "[dev-infra] starting qdrant server ..."
    mkdir -p "$DATA_ROOT/qdrant-server/storage"
    (cd "$DATA_ROOT/qdrant-server" && nohup ./qdrant >"$DATA_ROOT/qdrant.log" 2>&1 &)
    for _ in $(seq 1 20); do
      curl -s --max-time 1 http://127.0.0.1:6333/healthz >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
  curl -s --max-time 2 http://127.0.0.1:6333/healthz >/dev/null || echo "[dev-infra] 警告: qdrant 未就绪"
fi

# ---------- Redis ----------
if ! "$ENV_BIN/redis-cli" -p 6379 ping >/dev/null 2>&1; then
  echo "[dev-infra] starting redis ..."
  "$ENV_BIN/redis-server" --daemonize yes --port 6379 --dir "$REDIS_DIR" \
    --appendonly yes --logfile "$DATA_ROOT/redis.log"
fi

"$ENV_BIN/psql" -U edu -h 127.0.0.1 -p 5432 -d eduagents -c 'SELECT 1' >/dev/null
"$ENV_BIN/redis-cli" -p 6379 ping >/dev/null
echo "[dev-infra] postgres:5432 + redis:6379 就绪"
