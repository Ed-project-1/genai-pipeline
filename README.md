# genai-pipeline
cat > DEPLOY.md << 'EOF'
# Project Meridian — Deployment Guide

## Prerequisites
- AWS CLI configured
- Docker Desktop running (containerd DISABLED)
- Python 3.12

## One-time setup
```bash
# Create ECR repos
aws ecr create-repository --repository-name meridian-api --region us-east-1
aws ecr create-repository --repository-name meridian-ingestion --region us-east-1

# Create GitHub OIDC provider (if not exists)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

## Build and push Docker images
```bash
ECR_BASE="960828421512.dkr.ecr.us-east-1.amazonaws.com"

aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin $ECR_BASE

DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build \
  --platform linux/amd64 --no-cache \
  --provenance=false --sbom=false \
  -f Dockerfile.api \
  -t $ECR_BASE/meridian-api:latest .

DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build \
  --platform linux/amd64 --no-cache \
  --provenance=false --sbom=false \
  -f Dockerfile.ingestion \
  -t $ECR_BASE/meridian-ingestion:latest .

docker push $ECR_BASE/meridian-api:latest
docker push $ECR_BASE/meridian-ingestion:latest
```

## Deploy CloudFormation stack
```bash
ECR_BASE="960828421512.dkr.ecr.us-east-1.amazonaws.com"

aws cloudformation create-stack \
  --stack-name meridian-dev \
  --template-body file://infrastructure/cloudformation/main.yaml \
  --parameters \
    ParameterKey=Environment,ParameterValue=dev \
    ParameterKey=ProjectName,ParameterValue=meridian \
    ParameterKey=ECRImageUri,ParameterValue=${ECR_BASE}/meridian-api:latest \
    ParameterKey=IngestionECRImageUri,ParameterValue=${ECR_BASE}/meridian-ingestion:latest \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation wait stack-create-complete \
  --stack-name meridian-dev \
  --region us-east-1
```

## Remove VPC from Lambda functions (required for OpenSearch access)
```bash
aws lambda update-function-configuration \
  --function-name meridian-dev-ingestion \
  --vpc-config SubnetIds=[],SecurityGroupIds=[] \
  --region us-east-1

aws lambda wait function-updated \
  --function-name meridian-dev-ingestion --region us-east-1

aws lambda update-function-configuration \
  --function-name meridian-dev-api \
  --vpc-config SubnetIds=[],SecurityGroupIds=[] \
  --region us-east-1

aws lambda wait function-updated \
  --function-name meridian-dev-api --region us-east-1
```

## Set correct Bedrock model IDs
```bash
aws lambda update-function-configuration \
  --function-name meridian-dev-api \
  --region us-east-1 \
  --environment "Variables={
    OPENSEARCH_ENDPOINT=$(aws cloudformation describe-stacks --stack-name meridian-dev --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='OpenSearchEndpoint'].OutputValue" --output text),
    OPENSEARCH_INDEX=meridian-docs,
    BEDROCK_REGION=us-east-1,
    PRIMARY_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0,
    FALLBACK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0,
    RAW_DOCUMENTS_BUCKET=$(aws cloudformation describe-stacks --stack-name meridian-dev --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='RawDocumentsBucketName'].OutputValue" --output text),
    SESSION_TABLE=meridian-dev-sessions,
    DAILY_TOKEN_BUDGET=100000
  }"

aws lambda wait function-updated \
  --function-name meridian-dev-api --region us-east-1
```

## Fix IAM permissions for Bedrock inference profiles
```bash
aws iam put-role-policy \
  --role-name meridian-dev-api-lambda-role \
  --policy-name BedrockInferenceProfile \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
      "Resource":[
        "arn:aws:bedrock:us-east-1:960828421512:inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0",
        "arn:aws:bedrock:us-east-1:960828421512:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/*"
      ]
    }]
  }'

aws iam put-role-policy \
  --role-name meridian-dev-ingestion-lambda-role \
  --policy-name BedrockInferenceProfile \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Action":"bedrock:InvokeModel",
      "Resource":[
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:us-east-1::foundation-model/*"
      ]
    }]
  }'
```

## Create Cognito test user
```bash
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name meridian-dev --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name meridian-dev --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoClientId'].OutputValue" \
  --output text)

aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username testuser@meridian.com \
  --temporary-password 'Temp1234!' \
  --message-action SUPPRESS \
  --region us-east-1

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username testuser@meridian.com \
  --password 'Perm5678#' \
  --permanent \
  --region us-east-1
```

## Upload and ingest sample document
```bash
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name meridian-dev --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='RawDocumentsBucketName'].OutputValue" \
  --output text)

aws s3 cp data/meeting_memo.txt \
  s3://$BUCKET/uploads/ \
  --metadata 'classification=INTERNAL,uploaded_by=tutorial' \
  --region us-east-1

sleep 30

aws logs tail /aws/lambda/meridian-dev-ingestion \
  --follow --region us-east-1
```

## Test the pipeline
```bash
TOKEN=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id $CLIENT_ID \
  --auth-parameters USERNAME=testuser@meridian.com,PASSWORD='Perm5678#' \
  --region us-east-1 \
  --query "AuthenticationResult.IdToken" \
  --output text)

API=$(aws cloudformation describe-stacks \
  --stack-name meridian-dev --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='APIEndpoint'].OutputValue" \
  --output text)

curl -s -X POST $API/query \
  -H "Content-Type: application/json" \
  -H "Authorization: $TOKEN" \
  -d '{"question": "What budget was approved?", "classification": "INTERNAL"}' \
  | python3 -m json.tool
```

## Tear down — run this when done to stop all charges
```bash
aws cloudformation delete-stack \
  --stack-name meridian-dev \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name meridian-dev \
  --region us-east-1

echo "Stack deleted — all charges stopped"
```

## Known issues and fixes
1. OCI manifest error — disable containerd in Docker Desktop settings
2. Permission denied on handler.py — add chmod -R 755 in Dockerfile
3. OpenSearch timeout — remove VPC from Lambda functions
4. Bedrock ValidationException — add us. prefix to model IDs
5. Bedrock AccessDeniedException — add inference profile ARN to IAM policy
6. Cognito ! in password — use single quotes around password
EOF