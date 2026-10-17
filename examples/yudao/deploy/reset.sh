#!/bin/sh
# Recreate the yudao database from the schema scripts and restart the server.
# Discards every record in the local yudao database. Defaults to .q0-cache/yudao.
set -eu
cd "$(dirname "$0")"
: "${YUDAO_CACHE:=$(cd ../../.. && pwd)/.q0-cache/yudao}"
export YUDAO_CACHE
docker compose rm -sf server mysql >/dev/null
docker volume rm -f woa-yudao_mysql >/dev/null
docker compose up -d mysql
until [ "$(docker inspect -f '{{.State.Health.Status}}' woa-yudao-mysql-1)" = healthy ]; do sleep 3; done
# The entrypoint restarts mysqld after the init scripts; wait for the schema.
until docker exec woa-yudao-mysql-1 sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "select count(*) from information_schema.tables where table_schema=\"ruoyi-vue-pro\" and table_name=\"wms_warehouse\"" 2>/dev/null' | grep -q 1; do sleep 3; done
docker compose up -d server web
until docker logs woa-yudao-server-1 2>&1 | grep -qE 'Started YudaoServerApplication|Application run failed'; do sleep 3; done
docker logs woa-yudao-server-1 2>&1 | grep -E 'Started YudaoServerApplication|Application run failed'
