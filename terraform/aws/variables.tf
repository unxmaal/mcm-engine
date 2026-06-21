variable "aws_region" {
  description = "AWS region. Match the region used by ECR + RDS + ElastiCache + OpenSearch."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix for every named resource (RDS instance, ECR repo, App Runner service, etc.). Lower-kebab-case."
  type        = string
  default     = "mcm-engine"
}

variable "project_name" {
  description = "Value of MCM_PROJECT_NAME env var inside the running container."
  type        = string
  default     = "mcm-engine"
}

# --- VPC ---

variable "vpc_cidr" {
  description = "CIDR block for the engine's VPC."
  type        = string
  default     = "10.30.0.0/16"
}

variable "private_subnet_cidrs" {
  description = "Two CIDRs for the private subnets RDS + ElastiCache live in."
  type        = list(string)
  default     = ["10.30.10.0/24", "10.30.20.0/24"]
}

variable "public_subnet_cidrs" {
  description = "Two CIDRs for public subnets the App Runner VPC connector uses for egress."
  type        = list(string)
  default     = ["10.30.110.0/24", "10.30.120.0/24"]
}

# --- RDS Postgres ---

variable "rds_engine_version" {
  type    = string
  default = "16.4"
}

variable "rds_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "rds_allocated_storage_gb" {
  type    = number
  default = 20
}

variable "rds_db_name" {
  type    = string
  default = "mcm"
}

variable "rds_master_username" {
  type    = string
  default = "mcm"
}

variable "rds_master_password" {
  description = "RDS master password. Pass via TF_VAR_rds_master_password or a tfvars file outside source control."
  type        = string
  sensitive   = true
}

variable "rds_multi_az" {
  type    = bool
  default = false
}

# --- ElastiCache Redis ---

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "redis_num_cache_clusters" {
  type    = number
  default = 1
}

# --- OpenSearch Service ---

variable "opensearch_engine_version" {
  type    = string
  default = "OpenSearch_2.13"
}

variable "opensearch_instance_type" {
  type    = string
  default = "t3.small.search"
}

variable "opensearch_instance_count" {
  type    = number
  default = 1
}

variable "opensearch_volume_size_gb" {
  type    = number
  default = 10
}

# --- App Runner ---

variable "app_runner_cpu" {
  description = "App Runner vCPU. Default 0.25 vCPU is plenty for a knowledge engine."
  type        = string
  default     = "0.25 vCPU"
}

variable "app_runner_memory" {
  type    = string
  default = "0.5 GB"
}

variable "image_uri" {
  description = "Full ECR image URI to deploy (set after ./scripts/push-to-ecr.sh runs). e.g. 123.dkr.ecr.us-west-2.amazonaws.com/mcm-engine:phase4"
  type        = string
}
