# OpenSearch Service for search=opensearch (MCM2-23 AWS).

resource "aws_opensearch_domain" "search" {
  domain_name    = "${var.name_prefix}-os"
  engine_version = var.opensearch_engine_version

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = var.opensearch_instance_count
  }

  ebs_options {
    ebs_enabled = true
    volume_size = var.opensearch_volume_size_gb
    volume_type = "gp3"
  }

  vpc_options {
    subnet_ids         = [aws_subnet.private[0].id]
    security_group_ids = [aws_security_group.managed_services.id]
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  encrypt_at_rest {
    enabled = true
  }

  node_to_node_encryption {
    enabled = true
  }

  # IAM-based access; App Runner's IAM role gets es:* via aws_iam_policy
  # in app_runner.tf. Open access policy left to be a deliberate choice
  # of the operator — this module does NOT default to anonymous.
  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.app_runner_instance.arn }
      Action    = "es:*"
      Resource  = "arn:aws:es:${var.aws_region}:*:domain/${var.name_prefix}-os/*"
    }]
  })
}

output "opensearch_url" {
  description = "OpenSearch HTTPS endpoint."
  value       = "https://${aws_opensearch_domain.search.endpoint}"
}
