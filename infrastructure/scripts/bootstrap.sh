#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/bootstrap.sh
# Run this ONCE to set up the AWS prerequisites before the first deployment.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration — edit these ────────────────────────────────────────────────
PROJECT_NAME="meridian"
AWS_REGION="us-east-1"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ENVIRONMENT="${1:-dev}"    # pass 'staging' or 'prod' as arg

echo "════════════════════════════════════════"
echo "  Bootstrapping: ${PROJECT_NAME}-${ENVIRONMENT}"
echo "  Account: ${AWS_ACCOUNT_ID}"
echo "  Region:  ${AWS_REGION}"
echo "════════════════════════════════════════"

# ── 1. Create ECR repositories ────────────────────────────────────────────────
echo ""
echo "▶ Step 1: Creating ECR repositories..."

for REPO in "${PROJECT_NAME}-api" "${PROJECT_NAME}-ingestion"; do
  aws ecr describe-repositories --repository-names "${REPO}" 2>/dev/null || \
    aws ecr create-repository \
      --repository-name "${REPO}" \
      --image-scanning-configuration scanOnPush=true \
      --encryption-configuration encryptionType=AES256 \
      --region "${AWS_REGION}"
  echo "  ✅ ECR repo ready: ${REPO}"
done

# ── 2. Create GitHub Actions OIDC provider (run only once per account) ────────
echo ""
echo "▶ Step 2: Setting up GitHub Actions OIDC provider..."

OIDC_EXISTS=$(aws iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?ends_with(Arn, 'token.actions.githubusercontent.com')]" \
  --output text)

if [ -z "${OIDC_EXISTS}" ]; then
  aws iam create-open-id-connect-provider \
    --url "https://token.actions.githubusercontent.com" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1"
  echo "  ✅ OIDC provider created"
else
  echo "  ℹ️  OIDC provider already exists"
fi

# ── 3. Create IAM deploy role for GitHub Actions ──────────────────────────────
echo ""
echo "▶ Step 3: Creating GitHub Actions deploy role..."

GITHUB_ORG="Ed-project-1"    # ← CHANGE THIS
GITHUB_REPO="genai-pipeline"    # ← CHANGE THIS

TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:${GITHUB_ORG}/${GITHUB_REPO}:*"
        },
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
EOF
)

ROLE_NAME="${PROJECT_NAME}-${ENVIRONMENT}-github-deploy"

aws iam create-role \
  --role-name "${ROLE_NAME}" \
  --assume-role-policy-document "${TRUST_POLICY}" \
  2>/dev/null || echo "  ℹ️  Role already exists"

# Attach permissions needed by GitHub Actions to deploy
aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser"

aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn "arn:aws:iam::aws:policy/AWSCloudFormationFullAccess"

# Inline policy for Lambda, IAM, etc.
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "MeridianDeployPolicy" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {"Effect":"Allow","Action":["lambda:*","apigateway:*","s3:*","sqs:*",
       "dynamodb:*","cognito-idp:*","ec2:*","logs:*","iam:PassRole",
       "aoss:*"],"Resource":"*"}
    ]
  }'

ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
echo "  ✅ Deploy role: ${ROLE_ARN}"

# ── 4. Build and push placeholder images ─────────────────────────────────────
echo ""
echo "▶ Step 4: Building and pushing initial Docker images..."

ECR_BASE="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${ECR_BASE}"

docker build -f Dockerfile.api -t "${ECR_BASE}/${PROJECT_NAME}-api:latest" .
docker push "${ECR_BASE}/${PROJECT_NAME}-api:latest"

docker build -f Dockerfile.ingestion -t "${ECR_BASE}/${PROJECT_NAME}-ingestion:latest" .
docker push "${ECR_BASE}/${PROJECT_NAME}-ingestion:latest"

echo "  ✅ Images pushed to ECR"

# ── 5. Deploy the CloudFormation stack ───────────────────────────────────────
echo ""
echo "▶ Step 5: Deploying CloudFormation stack..."

aws cloudformation deploy \
  --template-file infrastructure/cloudformation/main.yaml \
  --stack-name "${PROJECT_NAME}-${ENVIRONMENT}" \
  --parameter-overrides \
    Environment="${ENVIRONMENT}" \
    ProjectName="${PROJECT_NAME}" \
    ECRImageUri="${ECR_BASE}/${PROJECT_NAME}-api:latest" \
    IngestionECRImageUri="${ECR_BASE}/${PROJECT_NAME}-ingestion:latest" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "${AWS_REGION}"

echo "  ✅ Stack deployed"

# ── 6. Output summary ─────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  Bootstrap complete! Key outputs:"
echo "════════════════════════════════════════"

aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-${ENVIRONMENT}" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table

echo ""
echo "Next steps:"
echo "  1. Add these GitHub Secrets to your repo:"
echo "     AWS_DEPLOY_ROLE_ARN = ${ROLE_ARN}"
echo "     AWS_DEV_DEPLOY_ROLE_ARN = ${ROLE_ARN}"
echo "  2. Upload the sample meeting memo:"
echo "     aws s3 cp data/meeting_memo.txt s3://${PROJECT_NAME}-${ENVIRONMENT}-raw-docs-${AWS_ACCOUNT_ID}/uploads/ \\"
echo "       --metadata 'classification=INTERNAL,uploaded_by=bootstrap,document_date=2024-09-12'"
echo "  3. Push to 'develop' branch to trigger the full CI/CD pipeline"
