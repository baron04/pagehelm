#!/bin/sh
# Prepare an Ubuntu 24.04 host with Docker for yudao: nginx, certbot, the firewall, SSH by
# key only, the deployment's directories and database password, and the site's certificate.
# Idempotent: run it again after a reinstall or to renew the site's configuration.
#
# usage: provision.sh <ssh-target> <domain>      e.g. provision.sh ubuntu@115.159.143.131 yudao.rscl.cc
set -eu
TARGET=${1:?ssh target}
DOMAIN=${2:?domain}
HERE=$(cd "$(dirname "$0")" && pwd)

scp -q "$HERE/nginx.site.conf" "$TARGET:/tmp/yudao.site.conf"
ssh "$TARGET" sudo sh -s -- "$DOMAIN" "$(ssh "$TARGET" id -un)" <<'REMOTE'
set -eu
DOMAIN=$1 OWNER=$2
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx certbot rsync ufw >/dev/null

# Docker for the deploying account; SSH by key only; the web ports.
usermod -aG docker "$OWNER"
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  > /etc/ssh/sshd_config.d/60-keys-only.conf
sshd -t
systemctl reload ssh
ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# The deployment: releases side by side, `current` the one served; a database password
# made here once and kept on the host only.
mkdir -p /opt/yudao/releases /var/www/certbot
chown -R "$OWNER" /opt/yudao
if [ ! -f /opt/yudao/.env ]; then
  umask 077
  printf 'YUDAO_DB_PASSWORD=%s\n' "$(openssl rand -hex 16)" > /opt/yudao/.env
  chown "$OWNER" /opt/yudao/.env
fi
grep -q '^YUDAO_DOMAIN=' /opt/yudao/.env || printf 'YUDAO_DOMAIN=%s\n' "$DOMAIN" >> /opt/yudao/.env

# The certificate: served over plain HTTP until it exists, then the full site.
rm -f /etc/nginx/sites-enabled/default
if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  printf 'server { listen 80; server_name %s; location /.well-known/acme-challenge/ { root /var/www/certbot; } }\n' \
    "$DOMAIN" > /etc/nginx/sites-available/yudao
  ln -sf /etc/nginx/sites-available/yudao /etc/nginx/sites-enabled/yudao
  nginx -t
  systemctl reload nginx
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email --deploy-hook "systemctl reload nginx"
fi
sed "s/__DOMAIN__/$DOMAIN/g" /tmp/yudao.site.conf > /etc/nginx/sites-available/yudao
ln -sf /etc/nginx/sites-available/yudao /etc/nginx/sites-enabled/yudao
nginx -t
systemctl reload nginx
systemctl enable --now certbot.timer >/dev/null
echo "provisioned $DOMAIN"
REMOTE
