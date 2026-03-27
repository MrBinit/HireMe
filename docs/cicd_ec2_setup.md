# CI/CD Setup For EC2 (Prod Only)

This repository now uses a prod-only GitHub Actions workflow:

- `.github/workflows/cicd-ec2.yml`

It builds a `linux/amd64` image, pushes to ECR, and deploys to EC2 over SSH.

## Configure GitHub Repository Variables

Set these under `Settings -> Secrets and variables -> Actions -> Variables`:

- `API_WORKERS` (example: `1`)
- `AWS_REGION` (example: `us-east-1`)
- `AWS_SECRETS_MANAGER_REGION` (example: `us-east-1`)
- `AWS_SECRETS_MANAGER_SECRET_ID` (example: `hireme/env`)
- `EC2_APP_DIR` (example: `~/HireMe`)
- `PUBLIC_BASE_URL` (optional but recommended, must be HTTPS for DocuSign webhook)

## Configure GitHub Repository Secrets

Set these under `Settings -> Secrets and variables -> Actions -> Secrets`:

- `AWS_GITHUB_ACTIONS_ROLE_ARN`
- `EC2_HOST`
- `EC2_PORT` (example: `22`)
- `EC2_SSH_KEY` (full PEM private key content)
- `EC2_USER` (example: `ubuntu`)

## Trigger Behavior

- Push to `main` deploys automatically.
- Manual run is available via `workflow_dispatch`.

## Local vs EC2 Differences

Local:

- Use `.env` / local overrides.
- Can use localhost URLs.
- Can run local DB/services.

EC2 Prod:

- Always pull image from ECR (no local build).
- Runtime secrets come from AWS Secrets Manager.
- Webhook URL should be public HTTPS (`PUBLIC_BASE_URL`).
- IAM role on EC2 must allow Secrets Manager, ECR pull, SQS, Bedrock, RDS access.

