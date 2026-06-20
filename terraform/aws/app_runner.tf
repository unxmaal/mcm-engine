# App Runner deployment (MCM2-24 AWS).
#
# Wires the ECR image into a runnable service with a VPC connector so it
# can reach the private RDS / ElastiCache / OpenSearch endpoints. The
# engine itself receives configuration via env vars — the same axes the
# orthogonal-config test (MCM2-19) exercises locally.

# --- IAM ---

# App Runner uses TWO roles:
#  - access role: lets the service pull from ECR.
#  - instance role: the application's own identity inside the container.
resource "aws_iam_role" "app_runner_access" {
  name = "${var.name_prefix}-app-runner-access"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "build.apprunner.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "app_runner_access_ecr" {
  role       = aws_iam_role.app_runner_access.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

resource "aws_iam_role" "app_runner_instance" {
  name = "${var.name_prefix}-app-runner-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "tasks.apprunner.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# OpenSearch IAM-based access — instance role gets es:* on our domain.
resource "aws_iam_role_policy" "app_runner_opensearch" {
  name = "${var.name_prefix}-app-runner-opensearch"
  role = aws_iam_role.app_runner_instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "es:*"
      Resource = "${aws_opensearch_domain.search.arn}/*"
    }]
  })
}

# --- VPC Connector (egress to managed services) ---

resource "aws_apprunner_vpc_connector" "main" {
  vpc_connector_name = "${var.name_prefix}-connector"
  subnets            = aws_subnet.private[*].id
  security_groups    = [aws_security_group.app_runner.id]
}

# --- Service ---

resource "aws_apprunner_service" "engine" {
  service_name = "${var.name_prefix}-engine"

  source_configuration {
    auto_deployments_enabled = false
    authentication_configuration {
      access_role_arn = aws_iam_role.app_runner_access.arn
    }
    image_repository {
      image_identifier      = var.image_uri
      image_repository_type = "ECR"
      image_configuration {
        port = "8080"
        runtime_environment_variables = {
          # MCM2-19 axes — each is independently switchable.
          MCM_PROJECT_NAME           = var.project_name
          MCM_HOST                   = "0.0.0.0"
          MCM_PORT                   = "8080"
          MCM_TRANSPORT              = "sse"
          # Backend selection. Edit the values, the image stays the same.
          MCM_BACKENDS_STORAGE       = "postgres"
          MCM_BACKENDS_COUNTERS      = "redis"
          MCM_BACKENDS_SEARCH        = "opensearch"
          MCM_BACKENDS_SESSION       = "embedded"
          # Endpoints (sensitive — App Runner stores these in plain text;
          # for production, replace with AWS Secrets Manager refs).
          MCM_POSTGRES_DSN           = "postgresql://${var.rds_master_username}:${var.rds_master_password}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.rds_db_name}"
          MCM_REDIS_URL              = "redis://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
          MCM_OPENSEARCH_URL         = "https://${aws_opensearch_domain.search.endpoint}"
        }
      }
    }
  }

  instance_configuration {
    cpu               = var.app_runner_cpu
    memory            = var.app_runner_memory
    instance_role_arn = aws_iam_role.app_runner_instance.arn
  }

  network_configuration {
    egress_configuration {
      egress_type       = "VPC"
      vpc_connector_arn = aws_apprunner_vpc_connector.main.arn
    }
  }

  health_check_configuration {
    protocol            = "HTTP"
    path                = "/healthz"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 1
    unhealthy_threshold = 3
  }

  observability_configuration {
    observability_enabled = false
  }

  tags = {
    Name = "${var.name_prefix}-engine"
  }
}

output "engine_url" {
  description = "Public App Runner URL — /healthz and /readyz answer here."
  value       = "https://${aws_apprunner_service.engine.service_url}"
}
