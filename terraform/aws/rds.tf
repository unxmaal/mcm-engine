# RDS Postgres for storage=postgres + counters=postgres (MCM2-23 AWS).
#
# Same DDL the local docker-compose Postgres uses runs here unchanged.
# The engine just consumes the DSN from MCM_TEST_POSTGRES_DSN / its config.

resource "aws_db_subnet_group" "rds" {
  name       = "${var.name_prefix}-rds-subnets"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${var.name_prefix}-rds-subnets" }
}

resource "aws_db_parameter_group" "pg16" {
  name        = "${var.name_prefix}-pg16"
  family      = "postgres16"
  description = "Parameters for ${var.name_prefix} Postgres"

  parameter {
    name  = "log_statement"
    value = "ddl"
  }
}

resource "aws_db_instance" "main" {
  identifier             = "${var.name_prefix}-postgres"
  engine                 = "postgres"
  engine_version         = var.rds_engine_version
  instance_class         = var.rds_instance_class
  allocated_storage      = var.rds_allocated_storage_gb
  storage_type           = "gp3"
  db_name                = var.rds_db_name
  username               = var.rds_master_username
  password               = var.rds_master_password
  multi_az               = var.rds_multi_az
  db_subnet_group_name   = aws_db_subnet_group.rds.name
  vpc_security_group_ids = [aws_security_group.managed_services.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name
  publicly_accessible    = false
  skip_final_snapshot    = true
  apply_immediately      = true
  storage_encrypted      = true
  tags                   = { Name = "${var.name_prefix}-postgres" }
}

# Engine consumes this DSN via MCM_TEST_POSTGRES_DSN (conformance run)
# or via mcm-engine.yaml backends.storage_options.dsn (production).
output "postgres_dsn" {
  description = "Postgres DSN for the engine. Includes credentials — treat as a secret."
  value       = "postgresql://${var.rds_master_username}:${var.rds_master_password}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.rds_db_name}"
  sensitive   = true
}
