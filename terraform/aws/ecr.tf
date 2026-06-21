# ECR repo for the mcm-engine image (MCM2-22 AWS).
#
# Apply this resource BEFORE pushing the image:
#   terraform apply -target=aws_ecr_repository.mcm

resource "aws_ecr_repository" "mcm" {
  name                 = var.name_prefix
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

output "ecr_repository_url" {
  description = "Set ECR_REPOSITORY in scripts/push-to-ecr.sh to this name."
  value       = aws_ecr_repository.mcm.repository_url
}
