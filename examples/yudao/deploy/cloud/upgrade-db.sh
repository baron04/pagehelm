#!/bin/sh
# Move the public host's yudao database to a new pinned release, keeping its data.
#
#   cloud/upgrade-db.sh <ssh-target> <upgrade.sql>
#
# upgrade.sql comes from `upgrade.py` (old and new checkouts and business schemas). The
# database is dumped first, to /opt/yudao/backups/pre-upgrade-<time>.sql.gz on the host;
# then the upgrade runs, and harden.sql's menu statements are applied again, since a menu
# the new release changes comes back as the upstream wrote it. Run it before deploy.sh
# brings the new server up.
set -eu
TARGET=$1
SQL=$2
HERE=$(cd "$(dirname "$0")" && pwd)
STAMP=$(date +%Y%m%d%H%M%S)
grep 'system_menu' "$HERE/../harden.sql" | grep -v '^--' > "/tmp/woa-menus-$STAMP.sql"
scp -q "$SQL" "$TARGET:/tmp/woa-upgrade-$STAMP.sql"
scp -q "/tmp/woa-menus-$STAMP.sql" "$TARGET:/tmp/woa-menus-$STAMP.sql"
rm -f "/tmp/woa-menus-$STAMP.sql"
ssh "$TARGET" sh -s -- "$STAMP" <<'REMOTE'
set -eu
STAMP=$1
mkdir -p /opt/yudao/backups
MYSQL='mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 ruoyi-vue-pro'
docker exec yudao-mysql-1 sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --databases ruoyi-vue-pro 2>/dev/null' \
  | gzip > "/opt/yudao/backups/pre-upgrade-$STAMP.sql.gz"
echo "backup /opt/yudao/backups/pre-upgrade-$STAMP.sql.gz $(du -h /opt/yudao/backups/pre-upgrade-$STAMP.sql.gz | cut -f1)"
docker exec -i yudao-mysql-1 sh -c "$MYSQL 2>&1 | grep -v Warning || true" < "/tmp/woa-upgrade-$STAMP.sql"
docker exec -i yudao-mysql-1 sh -c "$MYSQL 2>&1 | grep -v Warning || true" < "/tmp/woa-menus-$STAMP.sql"
rm -f "/tmp/woa-upgrade-$STAMP.sql" "/tmp/woa-menus-$STAMP.sql"
echo upgraded
REMOTE
