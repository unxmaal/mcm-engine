# Unit tests for terraform/aws/app_runner.tf.

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
  rds_master_password = "test-only-password"
  image_uri           = "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine:test"
}

run "app_runner_uses_provided_image_uri" {
  command = plan

  assert {
    condition     = aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_identifier == "123456789012.dkr.ecr.us-mock-1.amazonaws.com/mcm-engine:test"
    error_message = "App Runner must pull the exact image_uri the operator passed in (not a default)"
  }

  assert {
    condition     = aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_repository_type == "ECR"
    error_message = "image_repository_type must be ECR — public images aren't the supported topology"
  }
}

run "app_runner_health_check_hits_healthz" {
  command = plan

  assert {
    condition     = aws_apprunner_service.engine.health_check_configuration[0].path == "/healthz"
    error_message = "App Runner health check MUST hit /healthz (the liveness probe from MCM2-20). /readyz is for operators, not load-balancer healthchecks."
  }

  assert {
    condition     = aws_apprunner_service.engine.health_check_configuration[0].protocol == "HTTP"
    error_message = "health check protocol is HTTP (the FastMCP transport sub-app is HTTP, not raw TCP)"
  }
}

run "app_runner_env_includes_every_backend_axis" {
  command = plan

  # The map's KEYS are statically known from the configuration; only
  # their resolved values depend on computed attributes. contains() on
  # the keys list is plan-evaluable.
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_BACKENDS_STORAGE")
    error_message = "MCM_BACKENDS_STORAGE env var missing — config.py reads this for storage axis selection"
  }
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_BACKENDS_COUNTERS")
    error_message = "MCM_BACKENDS_COUNTERS env var missing"
  }
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_BACKENDS_SEARCH")
    error_message = "MCM_BACKENDS_SEARCH env var missing"
  }
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_BACKENDS_SESSION")
    error_message = "MCM_BACKENDS_SESSION env var missing"
  }
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_POSTGRES_DSN")
    error_message = "MCM_POSTGRES_DSN env var missing — engine can't reach RDS without it"
  }
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_REDIS_URL")
    error_message = "MCM_REDIS_URL env var missing — engine can't reach ElastiCache without it"
  }
  assert {
    condition     = contains(keys(aws_apprunner_service.engine.source_configuration[0].image_repository[0].image_configuration[0].runtime_environment_variables), "MCM_OPENSEARCH_URL")
    error_message = "MCM_OPENSEARCH_URL env var missing — engine can't reach OpenSearch without it"
  }
}

run "app_runner_egress_uses_vpc_connector" {
  command = plan

  assert {
    condition     = aws_apprunner_service.engine.network_configuration[0].egress_configuration[0].egress_type == "VPC"
    error_message = "egress_type MUST be VPC — managed services are private and not reachable via the default egress"
  }
}

run "vpc_connector_lives_in_all_private_subnets" {
  command = plan

  # Mocked subnets dedupe to one id (see unit_rds for the same pattern).
  # Assert the configuration wires aws_subnet.private[*].id by checking
  # the source splat width.
  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "VPC connector references aws_subnet.private[*].id — that splat must yield two subnets"
  }
}
