#!/usr/bin/env bash
# Create or update the API on AWS, then point web/config.js at it.
#   deploy/deploy.sh            (needs the AWS CLI logged in, and .env)
# Commit and push web/config.js afterwards if the API URL changed (first deploy).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
STACK=${STACK:-ddlc-expenses}
export AWS_REGION=${AWS_REGION:-ap-south-1}

aws cloudformation deploy --stack-name "$STACK" --template-file deploy/template.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "DashboardPassword=$DASHBOARD_PASSWORD" "TokenSecret=$TOKEN_SECRET" \
    "DiscordWebhookUrl=${DISCORD_WEBHOOK_URL:-}" "PublicUrl=${PUBLIC_URL:-https://expence.ddlc.ai}"

out() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
FN=$(out FunctionName); URL=$(out ApiUrl)

ZIP=$(mktemp -d)/api.zip
(cd backend && zip -q -r "$ZIP" ./*.py)
aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$ZIP" --query LastModified --output text
aws lambda wait function-updated --function-name "$FN"

printf 'window.EXPENSES_API = "%s";  // set by deploy/deploy.sh to the Lambda Function URL\n' "${URL%/}" > web/config.js
echo "API: ${URL%/}"
curl -s -o /dev/null -w "check: %{http_code} (401 = up, login required)\n" "${URL%/}/api/meta"
