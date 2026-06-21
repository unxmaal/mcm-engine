#!/usr/bin/env bash
# Push the Phase 4a container image to AWS ECR (MCM2-22).
#
# Run from the work system where AWS credentials are configured. The Mac
# mini cannot do this step — no AWS access.
#
# Required env vars:
#   AWS_REGION              — e.g., us-west-2
#   AWS_ACCOUNT_ID          — the 12-digit account id
#   ECR_REPOSITORY          — e.g., mcm-engine  (must already exist; see
#                             terraform/aws/ecr.tf)
#
# Optional:
#   IMAGE_TAG               — default "phase4" (matches docker build tag);
#                             set to a SHA or semver for release tags
#   DOCKERFILE_DIR          — default "."; where the Dockerfile lives
#
# Usage:
#   AWS_REGION=us-west-2 AWS_ACCOUNT_ID=123456789012 \
#     ECR_REPOSITORY=mcm-engine ./scripts/push-to-ecr.sh

set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID is required}"
: "${ECR_REPOSITORY:?ECR_REPOSITORY is required}"

IMAGE_TAG="${IMAGE_TAG:-phase4}"
DOCKERFILE_DIR="${DOCKERFILE_DIR:-.}"

REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${ECR_REPOSITORY}:${IMAGE_TAG}"

echo "==> aws ecr get-login-password (region=${AWS_REGION})"
aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${REGISTRY}"

echo "==> docker build (context=${DOCKERFILE_DIR}, tag=${IMAGE})"
docker build --platform linux/amd64 -t "${IMAGE}" "${DOCKERFILE_DIR}"

echo "==> docker push ${IMAGE}"
docker push "${IMAGE}"

echo "==> done. Image: ${IMAGE}"
echo "    Wire it into terraform/aws/app_runner.tf via var.image_uri."
