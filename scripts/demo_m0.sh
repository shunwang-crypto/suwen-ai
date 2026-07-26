#!/usr/bin/env bash
# M0-D1: 迁移从零建库成功 (在临时库上验证, 不动开发库)
set -euo pipefail
B=/home/shunw/miniforge3/envs/eduagents/bin
DB=eduagents_m0check
PASS=0; FAIL=0
check() { if eval "$2"; then echo "  PASS $1"; PASS=$((PASS+1)); else echo "  FAIL $1"; FAIL=$((FAIL+1)); fi; }

"$B/psql" -U edu -h 127.0.0.1 -d postgres -q -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
MIGRATION_DATABASE_URL="postgresql+asyncpg://edu:edu@127.0.0.1:5432/$DB" \
DATABASE_URL="postgresql+asyncpg://edu:edu@127.0.0.1:5432/$DB" \
  "$B/python" -m alembic upgrade head >/dev/null 2>&1

TABLES=$("$B/psql" -U edu -h 127.0.0.1 -d $DB -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
RLS=$("$B/psql" -U edu -h 127.0.0.1 -d $DB -tAc "SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity")
POLICIES=$("$B/psql" -U edu -h 127.0.0.1 -d $DB -tAc "SELECT count(*) FROM pg_policies")
ROLE=$("$B/psql" -U edu -h 127.0.0.1 -d $DB -tAc "SELECT count(*) FROM pg_roles WHERE rolname='edu_app' AND NOT rolsuper AND NOT rolbypassrls")

echo "== M0-D1 从零迁移验证 =="
check "21 张表建成 (实际 $TABLES)" "[ $TABLES -eq 21 ]"
check "16 张租户表 RLS ENABLE+FORCE (实际 $RLS)" "[ $RLS -eq 16 ]"
check "16 条隔离策略 (实际 $POLICIES)" "[ $POLICIES -eq 16 ]"
check "edu_app 角色存在且非超级用户/不可绕过RLS" "[ $ROLE -eq 1 ]"

"$B/psql" -U edu -h 127.0.0.1 -d postgres -q -c "DROP DATABASE $DB"
echo "== 结果: $PASS PASS / $FAIL FAIL =="
[ $FAIL -eq 0 ]
