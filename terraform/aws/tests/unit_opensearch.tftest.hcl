# Unit tests for terraform/aws/opensearch.tf.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-mock-1a", "us-mock-1b", "us-mock-1c"] }
  }
  override_resource {
    target = aws_iam_role.app_runner_instance
    values = { arn = "arn:aws:iam::123456789012:role/mcm-engine-app-runner-instance" }
  }
  override_resource {
    target = aws_iam_role.app_runner_access
    values = { arn = "arn:aws:iam::123456789012:role/mcm-engine-app-runner-access" }
  }
  override_resource {
    target = aws_apprunner_vpc_connector.main
    values = { arn = "arn:aws:apprunner:us-mock-1:123456789012:vpcconnector/mcm-engine-connector/1/0000" }
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
    values = { primary_endpoint_address = "mcm-engine-redis.mock.use1.cache.amazonaws.com" }
  }
}

variables {
  rds_master_password = "test-only"
  image_uri           = "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine:test"
}

run "opensearch_requires_https" {
  command = plan

  assert {
    condition     = aws_opensearch_domain.search.domain_endpoint_options[0].enforce_https == true
    error_message = "OpenSearch must enforce_https — adapter expects https:// endpoint"
  }
}

run "opensearch_uses_modern_tls_policy" {
  command = plan

  assert {
    condition     = aws_opensearch_domain.search.domain_endpoint_options[0].tls_security_policy == "Policy-Min-TLS-1-2-2019-07"
    error_message = "TLS policy floor is 1.2 — anything weaker is a finding"
  }
}

run "opensearch_encrypted_at_rest_and_node_to_node" {
  command = plan

  assert {
    condition     = aws_opensearch_domain.search.encrypt_at_rest[0].enabled == true
    error_message = "encrypt_at_rest must be enabled"
  }

  assert {
    condition     = aws_opensearch_domain.search.node_to_node_encryption[0].enabled == true
    error_message = "node_to_node_encryption must be enabled"
  }
}

run "opensearch_is_vpc_attached" {
  command = plan

  assert {
    condition     = length(aws_opensearch_domain.search.vpc_options[0].subnet_ids) >= 1
    error_message = "OpenSearch must be VPC-attached — no public endpoint"
  }

  assert {
    condition     = length(aws_opensearch_domain.search.vpc_options[0].security_group_ids) >= 1
    error_message = "OpenSearch must reference the managed-services SG"
  }
}

run "opensearch_access_policy_grants_only_app_runner_instance_role" {
  command = plan

  # The access policy is JSON-encoded; decode it before asserting shape.
  variables {
    expected_role_arn = "arn:aws:iam::123456789012:role/mcm-engine-app-runner-instance"
  }

  assert {
    condition     = jsondecode(aws_opensearch_domain.search.access_policies).Statement[0].Principal.AWS == "arn:aws:iam::123456789012:role/mcm-engine-app-runner-instance"
    error_message = "OpenSearch access policy must grant the App Runner instance role and ONLY that role"
  }

  assert {
    condition     = jsondecode(aws_opensearch_domain.search.access_policies).Statement[0].Action == "es:*"
    error_message = "OpenSearch access policy action is es:*"
  }
}
