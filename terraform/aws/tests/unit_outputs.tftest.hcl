# Unit tests: outputs the engine wiring + conformance scripts read.
#
# These outputs are the contract between this Terraform module and the
# rest of the project (scripts/run-aws-conformance.sh in particular).
# Renaming or removing any of them is a breaking change to that script.

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
    target = aws_apprunner_service.engine
    values = { service_url = "abc123.us-mock-1.awsapprunner.com" }
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
  override_resource {
    target = aws_ecr_repository.mcm
    values = { repository_url = "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine" }
  }
}

variables {
  rds_master_password = "test-only-password-xx"
  image_uri           = "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine:test"
}

run "postgres_dsn_output_is_a_postgresql_url" {
  command = plan

  assert {
    condition     = startswith(output.postgres_dsn, "postgresql://")
    error_message = "postgres_dsn output must start with postgresql:// — open_storage in migrate.py parses it as a URL"
  }
}

run "postgres_dsn_embeds_real_address_and_port" {
  command = plan

  assert {
    condition     = strcontains(output.postgres_dsn, "5432")
    error_message = "postgres_dsn must include port 5432"
  }

  assert {
    condition     = strcontains(output.postgres_dsn, "mcm-engine-postgres.mock.us-mock-1.rds.amazonaws.com")
    error_message = "postgres_dsn must include the RDS endpoint address"
  }
}

run "redis_url_output_is_a_redis_url" {
  command = plan

  assert {
    condition     = startswith(output.redis_url, "redis://")
    error_message = "redis_url must start with redis://"
  }

  assert {
    condition     = strcontains(output.redis_url, ":6379/")
    error_message = "redis_url must include the canonical port + db index suffix"
  }
}

run "opensearch_url_output_is_https" {
  command = plan

  assert {
    condition     = startswith(output.opensearch_url, "https://")
    error_message = "opensearch_url must be https — VPC-attached domain rejects http"
  }
}

run "engine_url_output_is_https_apprunner" {
  command = plan

  assert {
    condition     = startswith(output.engine_url, "https://")
    error_message = "engine_url must be https"
  }
}

run "ecr_repository_url_output_present" {
  command = plan

  assert {
    condition     = strcontains(output.ecr_repository_url, ".dkr.ecr.")
    error_message = "ecr_repository_url must be an ECR registry URL — scripts/push-to-ecr.sh reads it"
  }
}
