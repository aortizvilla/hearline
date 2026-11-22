#!/bin/bash
# First boot of the Hearline server (EC2 user data, Amazon Linux 2023, arm64).
# Installs Docker + compose, adds swap, then runs update.sh, which does every deploy.
set -euxo pipefail

dnf install -y docker unzip
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-aarch64"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# 2 GB swap: headroom for embedding + ffmpeg spikes on a 4 GB server
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon -a

mkdir -p /opt/hearline
REGION=$(TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60') && \
  curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)
FILES=$(aws ssm get-parameter --region "$REGION" --name /hearline/server-files \
  --query Parameter.Value --output text)
aws s3 cp --region "$REGION" "$FILES" /tmp/server.zip
unzip -o /tmp/server.zip -d /opt/hearline
chmod +x /opt/hearline/update.sh
/opt/hearline/update.sh
