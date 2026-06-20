#!/usr/bin/env bash
# Run the OpenTofu test suite for terraform/aws/.
#
# Single mode today: unit suites using `mock_provider` (plan-only,
# zero network). This is the primary TDD path — plan-time invariants
# of the IaC, asserted without any AWS or LocalStack at all.
#
# LocalStack-based integration is queued for a follow-up. The
# terraform/aws/ module provisions App Runner, which is pro-tier in
# every current LocalStack release; community LocalStack rejects the
# resource. When a license is available, drop a new
# tests/integration_*.tftest.hcl using `command = apply` with the
# LocalStack provider endpoints.
#
# Usage:
#   ./scripts/run-tofu-test.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="${REPO_ROOT}/terraform/aws"

cd "${TF_DIR}"

if [[ ! -d ".terraform" ]]; then
    echo "==> tofu init"
    tofu init -backend=false >/dev/null
fi

echo "==> Unit tests (mock_provider, plan-only)"
exec tofu test "$@"
