#!/bin/sh
# Export the local yudao database to $YUDAO_CACHE/snapshots/<name>.sql.gz,
# or restore it from that file with `snapshot.sh restore <name>`.
set -eu
: "${YUDAO_CACHE:?set YUDAO_CACHE}"
mkdir -p "$YUDAO_CACHE/snapshots"
if [ "${1:-}" = restore ]; then
  file="$YUDAO_CACHE/snapshots/${2:?snapshot name}.sql.gz"
  gunzip -c "$file" | docker exec -i woa-yudao-mysql-1 sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null'
  docker restart woa-yudao-server-1 >/dev/null  # drop cached permissions and dictionaries
  echo "restored $file"
else
  file="$YUDAO_CACHE/snapshots/${1:?snapshot name}.sql.gz"
  docker exec woa-yudao-mysql-1 sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --add-drop-database --databases ruoyi-vue-pro 2>/dev/null' | gzip > "$file"
  echo "exported $file ($(du -h "$file" | cut -f1))"
fi
