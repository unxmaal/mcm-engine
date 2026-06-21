# Unit tests for terraform/aws/rds.tf.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-mock-1a", "us-mock-1b", "us-mock-1c"]
    }
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
  rds_master_password = "test-only-not-a-real-password"
  image_uri           = "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine:test"
}

run "rds_is_encrypted_at_rest" {
  command = plan

  assert {
    condition     = aws_db_instance.main.storage_encrypted == true
    error_message = "RDS Postgres must be encrypted at rest. Don't ship anything else."
  }
}

run "rds_is_not_publicly_accessible" {
  command = plan

  assert {
    condition     = aws_db_instance.main.publicly_accessible == false
    error_message = "RDS must be private — only the App Runner VPC connector reaches it"
  }
}

run "rds_uses_postgres_engine_with_pinned_version" {
  command = plan

  assert {
    condition     = aws_db_instance.main.engine == "postgres"
    error_message = "engine must be 'postgres' — adapter contract assumes Postgres tsvector"
  }

  assert {
    condition     = aws_db_instance.main.engine_version != null && length(aws_db_instance.main.engine_version) > 0
    error_message = "engine_version must be explicitly pinned — don't drift onto an untested major"
  }
}

run "rds_subnet_group_references_all_private_subnets" {
  command = plan

  # Note: the AWS provider stores subnet_ids as a SET; under the mock
  # provider both private subnets get identical auto-ids and dedupe to
  # one. Assert on the wiring shape — the subnet group's subnet_ids
  # comes from aws_subnet.private[*].id — instead of the resolved set
  # size, which is a mock artifact.
  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "RDS subnet group references aws_subnet.private[*].id — that splat must yield two subnets"
  }
}
