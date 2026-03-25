#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
QUEUE_NAME="${QUEUE_NAME:-hireme-resume-parse}"
DLQ_NAME="${DLQ_NAME:-${QUEUE_NAME}-dlq}"

upsert_env() {
  local key="$1"
  local val="$2"
  local env_file=".env"
  local tmp_file
  tmp_file="$(mktemp)"
  touch "$env_file"
  grep -v "^${key}=" "$env_file" > "$tmp_file" || true
  echo "${key}=${val}" >> "$tmp_file"
  mv "$tmp_file" "$env_file"
}

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

upsert_env "AWS_REGION" "$AWS_REGION"
upsert_env "SQS_PARSE_QUEUE_URL" "$QUEUE_URL"
upsert_env "SQS_PARSE_QUEUE_NAME" "$QUEUE_NAME"

echo "Queue ready"
echo "QUEUE_URL=$QUEUE_URL"

echo "Healthcheck message send..."
aws sqs send-message \
  --region "$AWS_REGION" \
  --queue-url "$QUEUE_URL" \
  --message-body '{"event":"healthcheck"}' >/dev/null
echo "Healthcheck OK"
