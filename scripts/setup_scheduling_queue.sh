#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
QUEUE_NAME="${QUEUE_NAME:-hireme-interview-scheduling}"
DLQ_NAME="${DLQ_NAME:-${QUEUE_NAME}-dlq}"

echo "Creating DLQ: ${DLQ_NAME}"
DLQ_URL="$(aws sqs create-queue \
  --region "$AWS_REGION" \
  --queue-name "$DLQ_NAME" \
  --attributes "MessageRetentionPeriod=1209600,SqsManagedSseEnabled=true" \
  --query QueueUrl --output text)"

DLQ_ARN="$(aws sqs get-queue-attributes \
  --region "$AWS_REGION" \
  --queue-url "$DLQ_URL" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)"

echo "Creating main queue: ${QUEUE_NAME}"
QUEUE_URL="$(aws sqs create-queue \
  --region "$AWS_REGION" \
  --queue-name "$QUEUE_NAME" \
  --attributes "VisibilityTimeout=300,ReceiveMessageWaitTimeSeconds=20,SqsManagedSseEnabled=true" \
  --query QueueUrl --output text)"

aws sqs set-queue-attributes \
  --region "$AWS_REGION" \
  --queue-url "$QUEUE_URL" \
  --attributes "{\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"5\\\"}\"}"

if [[ ! -f "app/config/scheduling_config.yaml" ]]; then
  echo "Missing app/config/scheduling_config.yaml"
  exit 1
fi

sed -i '' "s#^  queue_name:.*#  queue_name: ${QUEUE_NAME}#g" app/config/scheduling_config.yaml
if grep -q "^  queue_url:" app/config/scheduling_config.yaml; then
  sed -i '' "s#^  queue_url:.*#  queue_url: \"${QUEUE_URL}\"#g" app/config/scheduling_config.yaml
else
  tmp_file="$(mktemp)"
  awk -v queue_url="$QUEUE_URL" '
    { print }
    /^  queue_name:/ {
      print "  queue_url: \"" queue_url "\""
    }
  ' app/config/scheduling_config.yaml > "$tmp_file"
  mv "$tmp_file" app/config/scheduling_config.yaml
fi

echo "Queue ready"
echo "QUEUE_URL=$QUEUE_URL"
echo "Updated app/config/scheduling_config.yaml (scheduling.queue_name, scheduling.queue_url)"
