#!/bin/bash
# Deploy on the server (run by bootstrap.sh on first boot, then by deploy.sh via SSM):
# fetch the latest files, image and settings from SSM, migrate, restart.
set -euo pipefail
cd /opt/hearline
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)
param() { aws ssm get-parameter --region "$REGION" --name "$1" --with-decryption \
  --query Parameter.Value --output text; }

# 1. this folder's files (compose, Caddyfile, this script) as of the latest deploy
aws s3 cp --region "$REGION" "$(param /hearline/server-files)" /tmp/server.zip
unzip -oq /tmp/server.zip -d /opt/hearline

# 2. settings: /hearline/config (from the stack) + /hearline/secrets (put_secrets.py)
umask 077
{ param /hearline/config; echo; param /hearline/secrets; echo; } > hearline.env
get() { grep -m1 "^$1=" hearline.env | cut -d= -f2-; }  # read a value without running it
ORIGIN_DOMAIN=$(get ORIGIN_DOMAIN); ORIGIN_SECRET=$(get ORIGIN_SECRET)
POSTGRES_PASSWORD=$(get POSTGRES_PASSWORD); LOG_GROUP=$(get LOG_GROUP)
echo "DATABASE_URL=postgresql+psycopg://hearline:${POSTGRES_PASSWORD}@db:5432/hearline" \
  >> hearline.env
# compose's own variables (image, Caddy, Postgres, logs)
cat > .env <<VARS
HEARLINE_IMAGE=$(param /hearline/image-uri)
ORIGIN_DOMAIN=${ORIGIN_DOMAIN}
ORIGIN_SECRET=${ORIGIN_SECRET}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
AWS_REGION=${REGION}
LOG_GROUP=${LOG_GROUP}
VARS

# 3. image, migrate, restart
IMAGE=$(param /hearline/image-uri)
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${IMAGE%%/*}"
docker compose pull --quiet
docker compose up -d db
docker compose run --rm migrate
# the Settings / landing-page voice samples: records only the missing ones (cents, first deploy only)
docker compose run --rm api python -m app.cli voice-previews || echo "voice previews: will retry next deploy"
docker compose up -d --remove-orphans api worker caddy
docker image prune -f
echo "deployed $IMAGE"
