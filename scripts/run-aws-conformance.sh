#!/usr/bin/env bash
# Run the cross-adapter conformance suite against AWS managed services
# instead of local docker-compose containers (MCM2-25).
#
# Acceptance: identical green to the docker-compose run. Any failure
# here that didn't fail locally means the StorageBackend / CounterStore
# / SearchBackend abstraction leaked something AWS-specific — fix the
# interface, not the test.
#
# Reads Terraform outputs from terraform/aws/ to populate the test
# env vars. Run from the work system with AWS credentials configured.
#
# Usage:
#   ./scripts/run-aws-conformance.sh
#
# Or override individual endpoints:
#   MCM_TEST_POSTGRES_DSN=... ./scripts/run-aws-conformance.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="${REPO_ROOT}/terraform/aws"

# Pull DSNs from Terraform unless the operator already exported them.
if [[ -z "${MCM_TEST_POSTGRES_DSN:-}" ]]; then
    MCM_TEST_POSTGRES_DSN="$(cd "${TF_DIR}" && terraform output -raw postgres_dsn)"
fi
if [[ -z "${MCM_TEST_REDIS_URL:-}" ]]; then
    MCM_TEST_REDIS_URL="$(cd "${TF_DIR}" && terraform output -raw redis_url)"
fi
if [[ -z "${MCM_TEST_OPENSEARCH_URL:-}" ]]; then
    MCM_TEST_OPENSEARCH_URL="$(cd "${TF_DIR}" && terraform output -raw opensearch_url)"
fi

export MCM_TEST_POSTGRES_DSN
export MCM_TEST_REDIS_URL
export MCM_TEST_OPENSEARCH_URL

# Mask the DSN in logs — it contains credentials.
masked_dsn="$(echo "${MCM_TEST_POSTGRES_DSN}" | sed 's|//[^@]*@|//<creds>@|')"
echo "==> Running AWS conformance against:"
echo "    storage  ${masked_dsn}"
echo "    counters ${MCM_TEST_REDIS_URL}"
echo "    search   ${MCM_TEST_OPENSEARCH_URL}"
echo ""

cd "${REPO_ROOT}"

# The same test files Phase 1-3 wrote against docker-compose now run
# against the AWS endpoints because they read MCM_TEST_* env vars.
exec uv run python -m pytest \
    tests/test_adapter_postgres_storage.py \
    tests/test_adapter_postgres_counters.py \
    tests/test_adapter_postgres_search.py \
    tests/test_adapter_redis_counters.py \
    tests/test_adapter_opensearch_search.py \
    tests/test_orthogonal_wiring.py \
    tests/test_migrate.py \
    "$@"
