#!/usr/bin/env bash
# EduAgents 恢复三件套 (与 scripts/backup.sh 配对)
#
# 用法:
#   bash scripts/restore.sh <stamp>            # 例: bash scripts/restore.sh 20260727-033000
#   bash scripts/restore.sh <备份目录绝对路径>
#   RESTORE_YES=1 bash scripts/restore.sh <stamp>   # 跳过交互确认 (自动化用, 慎用)
#
# 行为:
#   1) PG:      pg_restore --clean --if-exists (覆盖式恢复到既有库)
#   2) objects: 现有目录先移到 objects.pre-restore-<ts> 再解包 (可回退)
#   3) Qdrant:  POST /collections/{c}/snapshots/upload?priority=snapshot (整 collection 重建)
# 恢复前请先停 API 与 worker (活动连接会拖慢/阻塞 pg_restore 的 DROP)。
set -euo pipefail

ENV_BIN="${ENV_BIN:-/home/shunw/miniforge3/envs/eduagents/bin}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-edu}"
PGDATABASE="${PGDATABASE:-eduagents}"
DATA_ROOT="${DATA_ROOT:-$HOME/.local/share/eduagents}"
BACKUP_ROOT="${BACKUP_ROOT:-$DATA_ROOT/backups}"
OBJECTS_DIR="${LOCAL_STORAGE_DIR:-$DATA_ROOT/objects}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"

if [ $# -lt 1 ]; then
  echo "用法: bash scripts/restore.sh <stamp|备份目录>"
  echo "可用备份:"
  ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sed 's/^/  /' || echo "  (无)"
  exit 1
fi

SRC="$1"
[ -d "$SRC" ] || SRC="$BACKUP_ROOT/$1"
if [ ! -d "$SRC" ]; then
  echo "[restore] 错误: 找不到备份目录 $SRC"
  exit 1
fi
STAMP="$(basename "$SRC")"
echo "[restore] 备份集: $SRC"
ls -lh "$SRC" | sed 's/^/  /'

# ---------- 二次确认 (覆盖式操作) ----------
if [ "${RESTORE_YES:-0}" != "1" ]; then
  echo
  echo "!! 即将覆盖数据库 '$PGDATABASE'、对象目录 '$OBJECTS_DIR' 与 Qdrant collection。"
  echo "!! 请先停掉 API 与 worker。确认请输入大写 YES:"
  read -r CONFIRM
  if [ "$CONFIRM" != "YES" ]; then
    echo "[restore] 已取消 (输入的不是 YES)"
    exit 1
  fi
fi

# ---------- 1) PostgreSQL ----------
DUMP="$(ls "$SRC"/eduagents-*.dump 2>/dev/null | head -1 || true)"
if [ -n "$DUMP" ]; then
  echo "[restore] pg_restore --clean --if-exists <- $DUMP"
  # --no-owner: 恢复到任意同权角色; 迁移创建的 edu_app 角色/授权不在 dump 内容之外另行处理
  "$ENV_BIN/pg_restore" --clean --if-exists --no-owner \
    -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" "$DUMP"
  echo "[restore] PG 恢复完成"
else
  echo "[restore] 警告: 备份集中无 .dump, 跳过 PG"
fi

# ---------- 2) 对象存储目录 ----------
TARBALL="$(ls "$SRC"/objects-*.tar.gz 2>/dev/null | head -1 || true)"
if [ -n "$TARBALL" ]; then
  if [ -d "$OBJECTS_DIR" ]; then
    ASIDE="$OBJECTS_DIR.pre-restore-$(date +%Y%m%d-%H%M%S)"
    mv "$OBJECTS_DIR" "$ASIDE"
    echo "[restore] 现有对象目录已移至: $ASIDE (确认无误后可手动删除)"
  fi
  tar -xzf "$TARBALL" -C "$(dirname "$OBJECTS_DIR")"
  echo "[restore] objects 解包完成 -> $OBJECTS_DIR"
else
  echo "[restore] 警告: 备份集中无 objects tar, 跳过对象存储"
fi

# ---------- 3) Qdrant ----------
FOUND_SNAP=0
for f in "$SRC"/qdrant-*.snapshot; do
  [ -e "$f" ] || continue
  FOUND_SNAP=1
  base="$(basename "$f")"                       # qdrant-<collection>-<stamp>.snapshot
  c="${base#qdrant-}"; c="${c%-"$STAMP".snapshot}"
  echo "[restore] 上传快照重建 collection '$c' ..."
  curl -sf -X POST "$QDRANT_URL/collections/$c/snapshots/upload?priority=snapshot" \
    -F "snapshot=@$f" >/dev/null
  echo "[restore] qdrant ($c) 恢复完成"
done
[ "$FOUND_SNAP" = "1" ] || echo "[restore] 警告: 备份集中无 qdrant 快照, 跳过向量库"

echo "[restore] 全部完成。建议依次验证:"
echo "  $ENV_BIN/psql -U $PGUSER -h $PGHOST -d $PGDATABASE -c 'SELECT count(*) FROM users'"
echo "  curl -s $QDRANT_URL/collections | python3 -m json.tool"
echo "  重启 API/worker 后跑 make test-integration"
