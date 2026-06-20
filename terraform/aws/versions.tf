# Phase 4b — AWS reference Terraform for mcm-engine deployments.
#
# This is one valid topology. A user picking different defaults
# (db.t4g.medium, multi-AZ, encryption keys, etc.) edits these knobs
# but does not need to change the engine.
#
# Apply order:
#   terraform init
#   terraform apply -target=aws_ecr_repository.mcm
#   ./scripts/push-to-ecr.sh                       (push image)
#   terraform apply                                (everything else)
#
# This module is authored offline; `terraform apply` requires AWS
# credentials available on the work system.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
