#!/usr/bin/env bash
# make preheat — 会前预热 (提示词 §9.4)
#
# 步骤:
#   1) dev-infra 三服务健康: PostgreSQL / Redis / Qdrant
#   2) 端口探活: API (:8000) 与 web (:3000)
#   3) demo 账号真实登录一次 (curl, 预热 bcrypt/JWT/连接池)
#   4) 对黄金路径录制过的问题发一次 chat (预热模型通道/语义缓存/Qdrant 检索)
#   5) 打印服务状态表; 任一 FAIL 退出码非零
#
# 前置: make dev-infra && make seed (demo 账号由 seed_demo.py 创建)
# 可配: API_BASE / WEB_BASE / DEMO_EMAIL / DEMO_PASSWORD / PREHEAT_QUESTION
set -uo pipefail

ENV_BIN="${ENV_BIN:-/home/shunw/miniforge3/envs/eduagents/bin}"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"
WEB_BASE="${WEB_BASE:-http://127.0.0.1:3000}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
DEMO_EMAIL="${DEMO_EMAIL:-demo@eduagents.cn}"
DEMO_PASSWORD="${DEMO_PASSWORD:-demo-pass-123}"
# 黄金路径录制过的问题 (scripts/smoke_golden_path.py 同款, cassettes 已有录制)
PREHEAT_QUESTION="${PREHEAT_QUESTION:-拉格朗日余项的形式是什么?}"

declare -a ROWS
FAIL=0
row() { # row <name> <ok|FAIL|skip> <detail>
  ROWS+=("$(printf '%-28s %-6s %s' "$1" "$2" "$3")")
  [ "$2" = "FAIL" ] && FAIL=1
}

echo "== EduAgents 会前预热 =="

# ---------- 1) 基础设施 ----------
if "$ENV_BIN/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  row "postgres:5432" ok "pg_isready"
else
  row "postgres:5432" FAIL "pg_isready 失败 (make dev-infra)"
fi
if [ "$("$ENV_BIN/redis-cli" -p 6379 ping 2>/dev/null)" = "PONG" ]; then
  row "redis:6379" ok "PONG"
else
  row "redis:6379" FAIL "无 PONG (make dev-infra)"
fi
if curl -sf --max-time 3 "$QDRANT_URL/healthz" >/dev/null 2>&1; then
  NCOLL=$(curl -sf --max-time 3 "$QDRANT_URL/collections" \
    | "$ENV_BIN/python" -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["collections"]))' 2>/dev/null || echo "?")
  row "qdrant:6333" ok "healthz, ${NCOLL} collections"
else
  row "qdrant:6333" FAIL "healthz 不通 (make dev-infra)"
fi

# ---------- 2) API / web 端口 ----------
HEALTH_JSON=$(curl -sf --max-time 5 "$API_BASE/api/health" 2>/dev/null || true)
if [ -n "$HEALTH_JSON" ]; then
  GWMODE=$(printf '%s' "$HEALTH_JSON" \
    | "$ENV_BIN/python" -c 'import json,sys; print(json.load(sys.stdin).get("gateway_mode","?"))' 2>/dev/null || echo "?")
  row "api $API_BASE" ok "health ok, gateway_mode=$GWMODE"
else
  row "api $API_BASE" FAIL "/api/health 不通 (make api)"
fi
WEB_CODE=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$WEB_BASE" 2>/dev/null || echo 000)
if [ "$WEB_CODE" = "200" ]; then
  row "web $WEB_BASE" ok "HTTP 200"
else
  row "web $WEB_BASE" FAIL "HTTP $WEB_CODE (cd apps/web && npm run dev)"
fi

# ---------- 3) demo 账号登录 ----------
TOKEN=""
LOGIN_JSON=$(curl -sf --max-time 10 -X POST "$API_BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$DEMO_EMAIL\",\"password\":\"$DEMO_PASSWORD\"}" 2>/dev/null || true)
if [ -n "$LOGIN_JSON" ]; then
  TOKEN=$(printf '%s' "$LOGIN_JSON" \
    | "$ENV_BIN/python" -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
fi
if [ -n "$TOKEN" ]; then
  row "demo 登录 ($DEMO_EMAIL)" ok "access_token 已签发"
else
  row "demo 登录 ($DEMO_EMAIL)" FAIL "登录失败 (先 make seed)"
fi

# ---------- 4) chat 预热 (录制过的问题 -> 模型通道 + 语义缓存 + 检索) ----------
if [ -n "$TOKEN" ]; then
  KB_ID=$(curl -sf --max-time 10 "$API_BASE/api/kb" -H "Authorization: Bearer $TOKEN" \
    | "$ENV_BIN/python" -c 'import json,sys; kbs=json.load(sys.stdin); print(kbs[0]["id"] if kbs else "")' 2>/dev/null || true)
  if [ -n "$KB_ID" ]; then
    # SSE 流拉满 (max-time 兜底); 统计 token 事件数与是否 done
    SSE_OUT=$(curl -sN --max-time 60 -X POST "$API_BASE/api/chat" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d "{\"message\":\"$PREHEAT_QUESTION\",\"kb_id\":\"$KB_ID\"}" 2>/dev/null || true)
    N_TOKEN=$(printf '%s' "$SSE_OUT" | grep -c '^event: token' || true)
    if printf '%s' "$SSE_OUT" | grep -q '^event: done'; then
      row "chat 预热 (kb=${KB_ID:0:8}…)" ok "SSE 完整走完, $N_TOKEN 个 token 事件, 缓存已预填"
    elif [ "$N_TOKEN" -gt 0 ]; then
      row "chat 预热 (kb=${KB_ID:0:8}…)" FAIL "有 token 但未见 done (共 $N_TOKEN 个)"
    else
      row "chat 预热 (kb=${KB_ID:0:8}…)" FAIL "SSE 无 token 事件"
    fi
  else
    row "chat 预热" FAIL "demo 账号无知识库 (先 make seed)"
  fi
else
  row "chat 预热" skip "登录失败, 跳过"
fi

# ---------- 5) 状态表 ----------
echo
printf '%-28s %-6s %s\n' "服务/步骤" "状态" "详情"
printf '%s\n' "--------------------------------------------------------------"
for r in "${ROWS[@]}"; do printf '%s\n' "$r"; done
echo
if [ "$FAIL" = "0" ]; then
  echo "== preheat: 全部通过, 可以开演 =="
else
  echo "== preheat: 存在 FAIL, 按详情列处置后重跑 make preheat =="
fi
exit $FAIL
