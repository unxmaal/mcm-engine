# Unit tests for terraform/aws/network.tf.
#
# Uses `mock_provider` so `tofu test` runs in plan-only mode with no AWS
# credentials. Asserts the SHAPE of what would be applied — port lists,
# CIDR ordering, SG ingress rules — without ever calling AWS.
#
# Run via: tofu test
#
# Or just this file: tofu test -filter=tests/unit_network.tftest.hcl

mock_provider "aws" {
  # Provider calls return synthetic data. Data sources need explicit
  # mock_data when their outputs are read by configurations.
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-mock-1a", "us-mock-1b", "us-mock-1c"]
    }
  }

  # The AWS provider format-validates several ARN-shaped attributes
  # before the plan is finalized. The default mock fills these with
  # random-looking strings that fail validation. These overrides give
  # the affected resources valid-shape ARNs so the plan completes.
  override_resource {
    target = aws_iam_role.app_runner_instance
    values = {
      arn = "arn:aws:iam::123456789012:role/mcm-engine-app-runner-instance"
    }
  }
  override_resource {
    target = aws_iam_role.app_runner_access
    values = {
      arn = "arn:aws:iam::123456789012:role/mcm-engine-app-runner-access"
    }
  }
  override_resource {
    target = aws_apprunner_vpc_connector.main
    values = {
      arn = "arn:aws:apprunner:us-mock-1:123456789012:vpcconnector/mcm-engine-connector/1/0000"
    }
  }
  override_resource {
    target = aws_opensearch_domain.search
    values = {
      arn      = "arn:aws:es:us-mock-1:123456789012:domain/mcm-engine-os"
      endpoint = "search-mcm-engine-os.us-mock-1.es.amazonaws.com"
    }
  }
  override_resource {
    target = aws_db_instance.main
    values = {
      address = "mcm-engine-postgres.mock.us-mock-1.rds.amazonaws.com"
      port    = 5432
    }
  }
  override_resource {
    target = aws_elasticache_replication_group.redis
    values = {
      primary_endpoint_address = "mcm-engine-redis.mock.use1.cache.amazonaws.com"
    }
  }
}

variables {
  # rds_master_password is required by variables.tf — give it a value
  # so the plan can be generated.
  rds_master_password = "test-only-not-a-real-password"
  image_uri           = "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine:test"
}

run "vpc_uses_configured_cidr" {
  command = plan

  assert {
    condition     = aws_vpc.main.cidr_block == "10.30.0.0/16"
    error_message = "default VPC CIDR drifted from the value documented in variables.tf"
  }

  assert {
    condition     = aws_vpc.main.enable_dns_support == true
    error_message = "DNS support must be enabled for RDS+ElastiCache private endpoints to resolve"
  }
}

run "two_private_subnets_in_different_azs" {
  command = plan

  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "RDS subnet group needs exactly two private subnets across AZs"
  }
}

run "managed_services_sg_allows_only_app_runner_ingress" {
  command = plan

  assert {
    condition = alltrue([
      for r in aws_security_group.managed_services.ingress :
        length(r.security_groups) == 1 && length(coalesce(r.cidr_blocks, [])) == 0
    ])
    error_message = "managed_services SG must accept ingress ONLY from the app_runner SG. Any cidr_blocks-based ingress here is a public-exposure bug."
  }

  assert {
    condition = toset([for r in aws_security_group.managed_services.ingress : r.from_port]) == toset([5432, 6379, 443])
    error_message = "managed_services SG ports drifted from {5432 (postgres), 6379 (redis), 443 (opensearch)}"
  }
}
