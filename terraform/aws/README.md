# mcm-engine on AWS (Phase 4b)

Reference IaC for the `storage → RDS Postgres, counters → ElastiCache
Redis, search → OpenSearch Service, engine → App Runner` topology described
in `mcm2-scaling-plan.md` Phase 4b. The engine code does not change between
local docker-compose and AWS; only the env-var endpoints differ.

This module is authored on the Mac mini and tested with
[OpenTofu](https://opentofu.org/) (`tofu` CLI). HCL is shared between
OpenTofu and Terraform, but the test suite uses OpenTofu's
`mock_provider` feature so plan-time correctness can be asserted with no
AWS access. The `tofu apply` step runs on the work system.

## TDD

Every resource has plan-time assertions in `tests/*.tftest.hcl` that
exercise the IaC contract — encryption flags, ports, ingress rules,
output URL shapes, App Runner env-var presence — without any AWS
provider talking to a real API.

```bash
./scripts/run-tofu-test.sh
```

Today: 27 unit assertions across 6 test files (network, RDS, ElastiCache,
OpenSearch, App Runner, outputs). Adding a new resource means adding a
new `unit_<topic>.tftest.hcl` alongside it. Adding a new operator-facing
output means a new assertion in `unit_outputs.tftest.hcl`.

The mock_provider block at the top of each test file overrides ARN-shaped
attributes that the AWS provider format-validates before completing the
plan. Without those overrides the provider rejects the mock's random
auto-fills.

A LocalStack integration tier (`tofu apply` against an emulator instead
of real AWS) is left as follow-up — App Runner is pro-tier in current
LocalStack releases, so a community-tier emulator can't apply the full
module without a license token.

## Prerequisites

- AWS credentials with permissions to create VPC + RDS + ElastiCache +
  OpenSearch + App Runner + IAM + ECR.
- `tofu` 1.7+ (OpenTofu; Terraform 1.5+ also works but the test suite
  uses OpenTofu's `mock_provider` feature). `brew install opentofu`.
- `aws` CLI authenticated for the same account.
- `docker` available locally for the ECR push.

## Apply order

```bash
cd terraform/aws

# 1. Initialize providers.
tofu init

# 2. Stand up the ECR repo first so the push has a target.
tofu apply -target=aws_ecr_repository.mcm

# 3. Push the Phase 4a image to ECR.
#    (Reads AWS_REGION, AWS_ACCOUNT_ID, ECR_REPOSITORY env vars.)
cd ../..
AWS_REGION=us-west-2 AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) \
  ECR_REPOSITORY=mcm-engine IMAGE_TAG=phase4 ./scripts/push-to-ecr.sh

# 4. Apply the rest. Provide image_uri matching what was just pushed,
#    and rds_master_password (or use a TF_VAR_… env var).
cd terraform/aws
tofu apply \
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

After `tofu apply` succeeds:

```bash
ENGINE_URL=$(tofu output -raw engine_url)
curl -sf $ENGINE_URL/healthz   # → {"status":"ok"}
curl -sf $ENGINE_URL/readyz    # → {"status":"ok","checks":{...}}
```

Then run the same backend-parametrized conformance suite against the
managed services from the work system:

```bash
MCM_TEST_POSTGRES_DSN=$(tofu output -raw postgres_dsn) \
MCM_TEST_REDIS_URL=$(tofu output -raw redis_url) \
MCM_TEST_OPENSEARCH_URL=$(tofu output -raw opensearch_url) \
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
tofu destroy
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
