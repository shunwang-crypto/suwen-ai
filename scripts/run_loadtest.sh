#!/usr/bin/env bash
# M5 压测执行器: 健康检查 + gateway_mode 断言 + 账号池检查 + headless locust。
#
# 用法 (参数全走环境变量):
#   bash scripts/run_loadtest.sh
#   USERS=100 SPAWN_RATE=10 RUN_TIME=180s bash scripts/run_loadtest.sh
#   HOST=http://127.0.0.1:8001 bash scripts/run_loadtest.sh
#
# 压测目标是"用户自起的 stub 档实例", 默认端口 8001。
# 8000 端口上跑的是 replay 实例, 本脚本不碰它, 也不要把 HOST 指过去。
set -euo pipefail

REPO="/home/shunw/eduagents"
PY="/home/shunw/miniforge3/envs/eduagents/bin/python"
LOCUST="/home/shunw/miniforge3/envs/eduagents/bin/locust"
# conda libstdc++ 优先 (pymupdf/qdrant ABI 冲突), 跑本环境的 python/locust 都要带
export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib

USERS="${USERS:-30}"
SPAWN_RATE="${SPAWN_RATE:-5}"
RUN_TIME="${RUN_TIME:-90s}"
HOST="${HOST:-http://127.0.0.1:8001}"

ACCOUNTS="$REPO/loadtest/accounts.json"
EVIDENCE="${EVIDENCE_DIR:-/tmp/eduagents-evidence}"
STAMP="$(date +%Y%m%d-%H%M%S)"
PREFIX="$EVIDENCE/loadtest_stub_$STAMP"
mkdir -p "$EVIDENCE"

# stub 档 API 启动命令 (限流阈值故意调到极高: 本轮不测限流器)
START_CMD=$(cat <<'EOF'
cd /home/shunw/eduagents && export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib && \
GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
AUTH_RATE_LIMIT_PER_MINUTE=1000 API_RATE_LIMIT_PER_MINUTE=100000 \
/home/shunw/miniforge3/envs/eduagents/bin/python -m uvicorn eduagents_api.main:app --port 8001
EOF
)

# 种子命令: 必须与压测 API 同一套 stub env, 否则语料向量与查询向量口径不一致, 检索错乱
SEED_CMD=$(cat <<'EOF'
cd /home/shunw/eduagents && export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib && \
GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
/home/shunw/miniforge3/envs/eduagents/bin/python scripts/seed_loadtest.py \
  --count 10 --corpus corpus/calculus.md --out loadtest/accounts.json
EOF
)

echo "== EduAgents 压测 =="
echo "  HOST=$HOST  USERS=$USERS  SPAWN_RATE=$SPAWN_RATE  RUN_TIME=$RUN_TIME"

# ---------------------------------------------------------------- 1) 健康检查
echo "-- 健康检查 $HOST/api/health/ready"
if ! HEALTH=$(curl -fsS --max-time 5 "$HOST/api/health/ready" 2>/dev/null); then
  echo "错误: $HOST/api/health/ready 不可达。" >&2
  echo "压测需要一个专供压测的 stub 档 API 实例, 请另开一个终端执行 (完整命令):" >&2
  echo "" >&2
  echo "$START_CMD" >&2
  echo "" >&2
  echo "(8000 端口上是正在跑的 replay 实例, 不要动它。)" >&2
  exit 1
fi

# ------------------------------------------------- 2) gateway_mode 必须是 stub
GATEWAY_MODE=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("gateway_mode",""))' <<<"$HEALTH")
PG=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("checks",{}).get("postgres",""))' <<<"$HEALTH")
REDIS=$("$PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("checks",{}).get("redis",""))' <<<"$HEALTH")
echo "   gateway_mode=$GATEWAY_MODE postgres=$PG redis=$REDIS"

if [[ "$GATEWAY_MODE" != "stub" ]]; then
  echo "错误: $HOST 的 gateway_mode=$GATEWAY_MODE, 不是 stub。" >&2
  echo "GATEWAY_MODE 是进程启动时读入 settings 的, 不重启无法切 GATEWAY_MODE。" >&2
  echo "请另起一个 8001 的 stub 实例, 不要动 8000 的 replay 实例:" >&2
  echo "" >&2
  echo "$START_CMD" >&2
  echo "" >&2
  echo "然后重跑: HOST=http://127.0.0.1:8001 bash scripts/run_loadtest.sh" >&2
  exit 1
fi

if [[ "$PG" != "ok" || "$REDIS" != "ok" ]]; then
  echo "错误: 依赖不健康 (postgres=$PG redis=$REDIS)。先起基础设施: make dev-infra" >&2
  exit 1
fi

# ------------------------------------------------------------- 3) 账号池检查
if [[ ! -f "$ACCOUNTS" ]]; then
  echo "错误: 账号池不存在 $ACCOUNTS" >&2
  echo "先用与压测实例同一套 stub env 跑种子 (幂等, 可重复执行):" >&2
  echo "" >&2
  echo "$SEED_CMD" >&2
  echo "" >&2
  exit 1
fi
POOL=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1],encoding="utf-8"))["accounts"]))' "$ACCOUNTS")
SEED_EMB=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["settings"]["embedding_mode"])' "$ACCOUNTS")
echo "-- 账号池 $ACCOUNTS ($POOL 个账号, seed 时 embedding_mode=$SEED_EMB)"
if [[ "$SEED_EMB" != "stub" ]]; then
  echo "警告: 账号池是在 embedding_mode=$SEED_EMB 下种的, 与 stub 档压测实例口径不一致, 检索会错乱。" >&2
  echo "      建议先用下面的命令重种账号池再压测:" >&2
  echo "" >&2
  echo "$SEED_CMD" >&2
  echo "" >&2
fi

# ------------------------------------------------------------------ 4) 跑压测
mkdir -p "$EVIDENCE"
echo "-- 启动 locust (headless)"
cd "$REPO"
"$LOCUST" -f loadtest/locustfile.py \
  --headless \
  -u "$USERS" -r "$SPAWN_RATE" -t "$RUN_TIME" \
  --host "$HOST" \
  --csv "$PREFIX" --csv-full-history \
  --html "$PREFIX.html" \
  --only-summary

echo ""
echo "== 完成 =="
echo "  csv 前缀 : $PREFIX"
echo "  明细文件 : ${PREFIX}_stats.csv / ${PREFIX}_failures.csv / ${PREFIX}_stats_history.csv"
echo "  HTML 报告: $PREFIX.html"
echo "  关键行   : chat_ttfe / chat_ttft / chat_stream_total (Type=SSE) 的 p50 / p95"
