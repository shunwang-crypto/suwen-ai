#!/usr/bin/env bash
# EduAgents 备份三件套 (生产化审查 D1): PostgreSQL + 对象存储目录 + Qdrant 快照
#
# 产物: $BACKUP_ROOT/<stamp>/
#   eduagents-<stamp>.dump          pg_dump 自定义格式 (-Fc, 可选表恢复/并行恢复)
#   objects-<stamp>.tar.gz          本地对象存储目录整包 (LocalStorage; MinIO 部署见注①)
#   qdrant-<collection>-<stamp>.snapshot   Qdrant 官方快照 (逐 collection)
#   MANIFEST.txt                    清单 + sha256
# 轮转: 保留最近 $KEEP (默认 14) 份备份目录, 更旧的整目录删除。
# 幂等/可重入: stamp 精确到秒, 重复执行各自成套。
#
# cron 建议行 (crontab -e 由用户安装, 每天 03:30):
#   30 3 * * * /bin/bash /home/shunw/eduagents/scripts/backup.sh >> ~/.local/share/eduagents/backup.log 2>&1
#
# 注① MinIO 部署 (docker-compose) 时对象存储不在本地目录, 用 mc mirror 替代 tar:
#   mc alias set edu http://127.0.0.1:9000 <ACCESS_KEY> <SECRET_KEY>
#   mc mirror edu/eduagents "$BACKUP_ROOT/<stamp>/objects/"
# 注② compose 部署时 PG 在容器内, pg_dump 走宿主端口映射 (5432) 即可, 本脚本不变。
set -euo pipefail

ENV_BIN="${ENV_BIN:-/home/shunw/miniforge3/envs/eduagents/bin}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-edu}"          # 超级用户 (与 MIGRATION_DATABASE_URL 一致); 需要密码时用 PGPASSWORD 环境变量
PGDATABASE="${PGDATABASE:-eduagents}"
DATA_ROOT="${DATA_ROOT:-$HOME/.local/share/eduagents}"
BACKUP_ROOT="${BACKUP_ROOT:-$DATA_ROOT/backups}"
OBJECTS_DIR="${LOCAL_STORAGE_DIR:-$DATA_ROOT/objects}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
# 两个 collection: 业务向量 + 语义答案缓存 (answer_cache 惰性创建, 不存在则跳过)
QDRANT_COLLECTIONS="${QDRANT_COLLECTIONS:-eduagents_chunks answer_cache}"
KEEP="${KEEP:-14}"

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"
mkdir -p "$DEST"
echo "[backup] 目标目录: $DEST"

# ---------- 1) PostgreSQL: pg_dump -Fc ----------
DUMP="$DEST/eduagents-$STAMP.dump"
"$ENV_BIN/pg_dump" -Fc -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -f "$DUMP"
echo "[backup] pg_dump 完成: $(du -h "$DUMP" | cut -f1)"

# ---------- 2) 对象存储目录: tar ----------
if [ -d "$OBJECTS_DIR" ]; then
  TARBALL="$DEST/objects-$STAMP.tar.gz"
  tar -czf "$TARBALL" -C "$(dirname "$OBJECTS_DIR")" "$(basename "$OBJECTS_DIR")"
  echo "[backup] objects tar 完成: $(du -h "$TARBALL" | cut -f1)"
else
  echo "[backup] 警告: 对象目录不存在, 跳过 ($OBJECTS_DIR); MinIO 部署请用 mc mirror, 见脚本头注①"
fi

# ---------- 3) Qdrant: snapshot API (POST 创建 -> GET 下载 -> DELETE 服务端副本) ----------
for c in $QDRANT_COLLECTIONS; do
  if ! curl -sf --max-time 5 "$QDRANT_URL/collections/$c" >/dev/null 2>&1; then
    echo "[backup] 提示: collection '$c' 不存在或 Qdrant 不可达, 跳过 (answer_cache 惰性创建属正常)"
    continue
  fi
  SNAP_NAME="$(curl -sf -X POST "$QDRANT_URL/collections/$c/snapshots" \
    | "$ENV_BIN/python" -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')"
  curl -sf "$QDRANT_URL/collections/$c/snapshots/$SNAP_NAME" -o "$DEST/qdrant-$c-$STAMP.snapshot"
  curl -sf -X DELETE "$QDRANT_URL/collections/$c/snapshots/$SNAP_NAME" >/dev/null || true
  echo "[backup] qdrant snapshot ($c) 完成: $(du -h "$DEST/qdrant-$c-$STAMP.snapshot" | cut -f1)"
done

# ---------- 4) 清单 + 校验和 ----------
(cd "$DEST" && sha256sum ./* > MANIFEST.txt) || true
echo "[backup] 本套产物:"
du -h "$DEST"/* | sed 's/^/  /'
echo "[backup] 合计: $(du -sh "$DEST" | cut -f1)"

# ---------- 5) 轮转: 保留最近 $KEEP 套 ----------
mapfile -t SETS < <(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort)
N=${#SETS[@]}
if [ "$N" -gt "$KEEP" ]; then
  for old in "${SETS[@]:0:$((N - KEEP))}"; do
    rm -rf "$old"
    echo "[backup] 轮转删除: $old"
  done
fi
echo "[backup] 当前保留 $(ls -1d "$BACKUP_ROOT"/*/ | wc -l) 套 (上限 $KEEP)"
echo "[backup] 恢复: bash scripts/restore.sh $STAMP"
