# ElastiCache Redis for counters=redis (MCM2-23 AWS).

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.name_prefix}-redis-subnets"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.name_prefix}-redis"
  description                = "${var.name_prefix} counters CounterStore"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = var.redis_num_cache_clusters
  port                       = 6379
  parameter_group_name       = "default.redis7"
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.managed_services.id]
  automatic_failover_enabled = var.redis_num_cache_clusters > 1
  apply_immediately          = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = false
}

output "redis_url" {
  description = "Redis URL for the engine."
  value       = "redis://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
}
