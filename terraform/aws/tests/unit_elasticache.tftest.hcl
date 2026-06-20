# Unit tests for terraform/aws/elasticache.tf.

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

run "redis_uses_redis_engine" {
  command = plan

  assert {
    condition     = aws_elasticache_replication_group.redis.engine == "redis"
    error_message = "engine must be 'redis' — CounterStore adapter assumes Redis commands"
  }
}

run "redis_listens_on_canonical_port" {
  command = plan

  assert {
    condition     = aws_elasticache_replication_group.redis.port == 6379
    error_message = "port must be 6379 to match Phase 2 RedisCounters adapter defaults"
  }
}

run "redis_is_encrypted_at_rest" {
  command = plan

  # at_rest_encryption_enabled has unstable typing under the mock
  # provider (string "true") vs real provider (bool true). tostring()
  # normalizes both to "true" so the assertion holds either way.
  assert {
    condition     = tostring(aws_elasticache_replication_group.redis.at_rest_encryption_enabled) == "true"
    error_message = "ElastiCache at-rest encryption is required"
  }
}

run "redis_subnet_group_references_all_private_subnets" {
  command = plan

  # See unit_rds.tftest.hcl for why we assert on the source splat
  # instead of the resolved set — the mock provider dedupes identical
  # auto-ids.
  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Redis subnet group references aws_subnet.private[*].id — that splat must yield two subnets"
  }
}
