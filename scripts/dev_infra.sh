#!/usr/bin/env bash
# 本地开发基础设施 (Conda 版 PostgreSQL、Redis 与 Qdrant)
# 幂等: 重复执行只会确保服务在运行
set -euo pipefail

ENV_BIN="${PY_ENV:-$HOME/miniforge3/envs/eduagents}/bin"
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
    (
      cd "$DATA_ROOT/qdrant-server"
      nohup ./qdrant >"$DATA_ROOT/qdrant.log" 2>&1 </dev/null &
      disown
    )
    for _ in $(seq 1 20); do
      curl -s --max-time 1 http://127.0.0.1:6333/healthz >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
  curl -s --max-time 2 http://127.0.0.1:6333/healthz >/dev/null || echo "[dev-infra] 警告: qdrant 未就绪"
fi

# ---------- Redis ----------
# conda-forge 无 aarch64 redis 构建, 改用系统 redis (apt 安装)。
# 清除 conda 的 LD_LIBRARY_PATH, 避免系统二进制误加载冲突的 conda 共享库。
REDIS_CLI=(env -u LD_LIBRARY_PATH redis-cli)
REDIS_SERVER=(env -u LD_LIBRARY_PATH redis-server)
if ! timeout 2 "${REDIS_CLI[@]}" -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
  echo "[dev-infra] starting redis ..."
  "${REDIS_SERVER[@]}" --daemonize yes --port 6379 --dir "$REDIS_DIR" \
    --appendonly yes --logfile "$DATA_ROOT/redis.log"
fi

"$ENV_BIN/psql" -U edu -h 127.0.0.1 -p 5432 -d eduagents -c 'SELECT 1' >/dev/null
timeout 2 "${REDIS_CLI[@]}" -h 127.0.0.1 -p 6379 ping >/dev/null
echo "[dev-infra] postgres:5432 + redis:6379 就绪"
