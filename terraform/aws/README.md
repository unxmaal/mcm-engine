# mcm-engine on AWS (Phase 4b)

Reference Terraform for the `storage → RDS Postgres, counters → ElastiCache
Redis, search → OpenSearch Service, engine → App Runner` topology described
in `mcm2-scaling-plan.md` Phase 4b. The engine code does not change between
local docker-compose and AWS; only the env-var endpoints differ.

This module is authored on the Mac mini; the `terraform apply` runs on the
work system where AWS credentials live. No piece of this requires
write-access to AWS to author — only to deploy.

## Prerequisites

- AWS credentials with permissions to create VPC + RDS + ElastiCache +
  OpenSearch + App Runner + IAM + ECR.
- `terraform` 1.5+.
- `aws` CLI authenticated for the same account.
- `docker` available locally for the ECR push.

## Apply order

```bash
cd terraform/aws

# 1. Initialize providers.
terraform init

# 2. Stand up the ECR repo first so the push has a target.
terraform apply -target=aws_ecr_repository.mcm

# 3. Push the Phase 4a image to ECR.
#    (Reads AWS_REGION, AWS_ACCOUNT_ID, ECR_REPOSITORY env vars.)
cd ../..
AWS_REGION=us-west-2 AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) \
  ECR_REPOSITORY=mcm-engine IMAGE_TAG=phase4 ./scripts/push-to-ecr.sh

# 4. Apply the rest. Provide image_uri matching what was just pushed,
#    and rds_master_password (or use a TF_VAR_… env var).
cd terraform/aws
terraform apply \
  -var "image_uri=${AWS_ACCOUNT_ID}.dkr.ecr.us-west-2.amazonaws.com/mcm-engine:phase4" \
  -var "rds_master_password=$(openssl rand -base64 24 | tr -d /=+ | head -c 24)"
```

## What gets provisioned

| Resource | Why |
|----------|-----|
| VPC (10.30.0.0/16) + 2 private + 2 public subnets | Isolates the managed services. App Runner connector lives in private. |
| Security groups (managed-services, app-runner) | Managed-services SG accepts ingress only from app-runner SG on 5432/6379/443. |
| ECR repository (`mcm-engine`) | Hosts the Phase 4a image. |
| RDS Postgres 16, db.t4g.micro (single-AZ) | `storage=postgres` + `counters=postgres` backend. |
| ElastiCache Redis 7.1, cache.t4g.micro | `counters=redis` backend. |
| OpenSearch Service 2.13, t3.small.search | `search=opensearch` backend. Domain is VPC-attached. |
| App Runner service | Runs the engine. /healthz drives its health check; /readyz is operator-visible. |
| App Runner VPC Connector | Lets the service reach the three managed services in private subnets. |
| IAM roles (access + instance) | Access role pulls from ECR; instance role gets `es:*` on the OpenSearch domain. |

## Verifying the deployment (MCM2-25)

After `terraform apply` succeeds:

```bash
ENGINE_URL=$(terraform output -raw engine_url)
curl -sf $ENGINE_URL/healthz   # → {"status":"ok"}
curl -sf $ENGINE_URL/readyz    # → {"status":"ok","checks":{...}}
```

Then run the same backend-parametrized conformance suite against the
managed services from the work system:

```bash
MCM_TEST_POSTGRES_DSN=$(terraform output -raw postgres_dsn) \
MCM_TEST_REDIS_URL=$(terraform output -raw redis_url) \
MCM_TEST_OPENSEARCH_URL=$(terraform output -raw opensearch_url) \
  uv run python -m pytest tests/test_adapter_postgres_storage.py \
                          tests/test_adapter_postgres_counters.py \
                          tests/test_adapter_postgres_search.py \
                          tests/test_adapter_redis_counters.py \
                          tests/test_adapter_opensearch_search.py \
                          tests/test_orthogonal_wiring.py \
                          -v
```

Acceptance: identical green to the docker-compose run. If a test fails
that didn't fail locally, the StorageBackend/SearchBackend abstraction
leaked something AWS-specific — fix the interface, don't paper over it.

## Cleaning up

```bash
terraform destroy
```

App Runner services bill per second; the t4g.micro RDS + cache.t4g.micro
combination is roughly $40–50/month when idle. Don't leave it running
unless it's actually serving traffic.

## Production hardening (deliberately out of scope here)

- **Secrets Manager** for `MCM_POSTGRES_DSN` instead of plaintext env vars.
- **Multi-AZ RDS** (`rds_multi_az=true`).
- **Auto-scaling** App Runner config.
- **CloudFront** in front of App Runner if latency matters.
- **PrivateLink** for OpenSearch instead of VPC-embedded domain.
- **CloudWatch alarms** on /readyz failure, RDS CPU, Redis evictions.

The reference module here is the smallest thing that proves the engine
runs unchanged on managed AWS services. Hardening is the operator's job.
