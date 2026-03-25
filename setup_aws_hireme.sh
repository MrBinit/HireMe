#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
APP_NAME="${APP_NAME:-hireme}"

aws sts get-caller-identity >/dev/null

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
RAND="$(date +%s)"

S3_BUCKET="${APP_NAME}-cv-${ACCOUNT_ID}-${RAND}"
MAIN_QUEUE_NAME="${APP_NAME}-resume-parse"
DLQ_NAME="${APP_NAME}-resume-parse-dlq"

echo "Creating S3 bucket: ${S3_BUCKET} (${AWS_REGION})"

if [ "${AWS_REGION}" = "us-east-1" ]; then
  aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${AWS_REGION}"
else
  aws s3api create-bucket \
    --bucket "${S3_BUCKET}" \
    --region "${AWS_REGION}" \
    --create-bucket-configuration LocationConstraint="${AWS_REGION}"
fi

aws s3api put-public-access-block \
  --bucket "${S3_BUCKET}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption \
  --bucket "${S3_BUCKET}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-bucket-versioning \
  --bucket "${S3_BUCKET}" \
  --versioning-configuration Status=Enabled

echo "Creating SQS queues"

DLQ_URL="$(aws sqs create-queue \
  --queue-name "${DLQ_NAME}" \
  --attributes "MessageRetentionPeriod=1209600,SqsManagedSseEnabled=true" \
  --query QueueUrl --output text)"

DLQ_ARN="$(aws sqs get-queue-attributes \
  --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' --output text)"

MAIN_URL="$(aws sqs create-queue \
  --queue-name "${MAIN_QUEUE_NAME}" \
  --attributes "VisibilityTimeout=60,ReceiveMessageWaitTimeSeconds=20,SqsManagedSseEnabled=true" \
  --query QueueUrl --output text)"

aws sqs set-queue-attributes \
  --queue-url "${MAIN_URL}" \
  --attributes RedrivePolicy="{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"5\"}"

touch .env

upsert_env() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}=" .env; then
    sed -i '' "s#^${key}=.*#${key}=${val}#g" .env
  else
    echo "${key}=${val}" >> .env
  fi
}

upsert_env "AWS_REGION" "${AWS_REGION}"
upsert_env "AWS_S3_BUCKET" "${S3_BUCKET}"
upsert_env "SQS_PARSE_QUEUE_URL" "${MAIN_URL}"

echo "ok" > /tmp/hireme-healthcheck.txt

aws s3api put-object \
  --bucket "${S3_BUCKET}" \
  --key "healthcheck.txt" \
  --body /tmp/hireme-healthcheck.txt \
  --content-type "text/plain" >/dev/null

aws sqs send-message \
  --queue-url "${MAIN_URL}" \
  --message-body '{"event_type":"healthcheck"}' >/dev/null

echo "Done"
echo "AWS_REGION=${AWS_REGION}"
echo "AWS_S3_BUCKET=${S3_BUCKET}"
echo "SQS_PARSE_QUEUE_URL=${MAIN_URL}"
