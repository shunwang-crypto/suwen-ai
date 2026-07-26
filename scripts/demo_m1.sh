#!/usr/bin/env bash
# M1 DoD 汇总验证: 基础设施 + 单元 + 集成 (SSE/越权/usage 归因在集成测试内断言)
set -euo pipefail
B=/home/shunw/miniforge3/envs/eduagents/bin
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  PASS $1"; PASS=$((PASS+1)); else echo "  FAIL $1"; FAIL=$((FAIL+1)); fi; }

echo "== M1 DoD 验证 =="
check "dev-infra: postgres+redis 可用" "bash scripts/dev_infra.sh"
check "迁移到 head" "$B/python -m alembic upgrade head"
check "lint 全过" "$B/python -m ruff check packages apps tests alembic"
check "单元测试 (网关四模式/熔断/时钟/JWT)" "$B/python -m pytest tests/unit -q"
check "集成测试 (M1-D2 SSE / M1-D4 usage归因 / M1-D5 越权+RLS池复用 / U-A3 重放拒绝)" "INTEGRATION=1 $B/python -m pytest tests/integration -q"

echo "== 结果: $PASS PASS / $FAIL FAIL =="
echo "(M1-D1 docker-compose 全绿: 待 Docker daemon DNS 修复后 make up 验证, 见 README)"
[ $FAIL -eq 0 ]
