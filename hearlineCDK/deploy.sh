#!/bin/bash
# Deploy Hearline to AWS: one command for the first deploy and every one after.
#   1. keys for signed audio links + the CloudFront->server secret (once, hearlineCDK/.secrets/)
#   2. secrets -> SSM Parameter Store
#   3. frontend build (with the Clerk key)
#   4. cdk deploy: infrastructure, backend image, server files, frontend upload
#   5. tell the server to update (pull image, migrate, restart)
# Needs: AWS credentials (aws sts get-caller-identity works), Docker running, Node, Python 3.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
set -a; . ./config.env; set +a
: "${DOMAIN:?set DOMAIN in hearlineCDK/config.env}"
: "${CLERK_PUBLISHABLE_KEY:?set CLERK_PUBLISHABLE_KEY in hearlineCDK/config.env}"
: "${CLERK_ISSUER:?set CLERK_ISSUER in hearlineCDK/config.env}"
export AWS_REGION AWS_DEFAULT_REGION=$AWS_REGION

aws sts get-caller-identity --query Account --output text >/dev/null \
  || { echo "No AWS credentials: run 'aws configure' or 'aws sso login'"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Start Docker first (it builds the backend image)"; exit 1; }

# 1. keys (gitignored)
mkdir -p .secrets && chmod 700 .secrets
[ -f .secrets/cloudfront-private.pem ] || {
  openssl genrsa -out .secrets/cloudfront-private.pem 2048 2>/dev/null
  openssl rsa -pubout -in .secrets/cloudfront-private.pem -out .secrets/cloudfront-public.pem 2>/dev/null; }
[ -f .secrets/origin-secret ] || openssl rand -hex 32 > .secrets/origin-secret

# 2. secrets
"$ROOT/hearline/backend/.venv/bin/python" -I scripts/put_secrets.py "$AWS_REGION"

# 3. frontend
(cd "$ROOT/hearline/frontend" && VITE_CLERK_PUBLISHABLE_KEY=$CLERK_PUBLISHABLE_KEY npm run build)

# 4. infrastructure
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$DOMAIN" \
  --query "HostedZones[?Name=='$DOMAIN.'].Id | [0]" --output text | sed 's|/hostedzone/||')
[ "$ZONE_ID" != "None" ] || { echo "No Route 53 hosted zone for $DOMAIN"; exit 1; }
[ -d cdk/.venv ] || python3 -m venv cdk/.venv
cdk/.venv/bin/pip install -q -r cdk/requirements.txt
export PATH="$PWD/cdk/.venv/bin:$PATH"
CDK="npx --yes aws-cdk@2.1000.0"
aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1 \
  || (cd cdk && $CDK bootstrap)
(cd cdk && $CDK deploy Hearline --require-approval never \
  -c region="$AWS_REGION" -c domain="$DOMAIN" -c hostedZoneId="$ZONE_ID" \
  -c instanceType="$INSTANCE_TYPE" -c clerkIssuer="$CLERK_ISSUER" \
  -c adminUserIds="$ADMIN_USER_IDS" -c dailyBudgetUsd="$DAILY_BUDGET_USD" \
  -c alertEmail="${ALERT_EMAIL:-}" -c monthlyBudgetUsd="${MONTHLY_BUDGET_USD:-60}" \
  -c cloudfrontPublicKey="$(cat .secrets/cloudfront-public.pem)" \
  -c originSecret="$(cat .secrets/origin-secret)" \
  -c clerkDns="$(python3 -c 'import json;print(json.dumps(json.load(open("clerk-dns.json"))["records"]))')" \
  --outputs-file .secrets/outputs.json)

# 5. update the server (on the very first deploy its own boot script does this)
INSTANCE=$(python3 -c 'import json;print(json.load(open(".secrets/outputs.json"))["Hearline"]["InstanceId"])')
if aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE" \
     --query 'InstanceInformationList[0].PingStatus' --output text | grep -q Online; then
  CMD=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
    --parameters 'commands=["/opt/hearline/update.sh"]' --query Command.CommandId --output text)
  aws ssm wait command-executed --command-id "$CMD" --instance-id "$INSTANCE" || true
  aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" \
    --query '[Status, StandardOutputContent, StandardErrorContent]' --output text | tail -20
else
  echo "Server is booting: it installs itself (~5 min). Logs: CloudWatch log group /hearline"
fi
echo "Done: https://$DOMAIN"
