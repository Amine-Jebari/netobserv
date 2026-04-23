# ═════════════════════════════════════════════════════════════════════════════
#  Amazon OpenSearch Service — AWS counterpart to the local OpenSearch container.
#
#  Single-node domain tuned for the demo profile: t3.small.search, private VPC
#  placement, fine-grained access control, HTTPS enforced, node-to-node and
#  at-rest encryption enabled. Scale up `instance_count` or bump the instance
#  type for production — the consumer code is unchanged.
# ═════════════════════════════════════════════════════════════════════════════

variable "opensearch_domain_name" {
  description = "OpenSearch Service domain name."
  type        = string
  default     = "netobserv-search"
}

variable "opensearch_engine_version" {
  description = "OpenSearch engine version."
  type        = string
  default     = "OpenSearch_2.13"
}

variable "opensearch_instance_type" {
  description = "Data node instance type."
  type        = string
  default     = "t3.small.search"
}

variable "opensearch_instance_count" {
  description = "Number of data nodes (1 for the single-node demo)."
  type        = number
  default     = 1
}

variable "opensearch_volume_gb" {
  description = "EBS volume size per data node (GB)."
  type        = number
  default     = 50
}

variable "opensearch_master_username" {
  description = "Master user for fine-grained access control. Password is sourced from Secrets Manager."
  type        = string
  default     = "netobserv-admin"
}

variable "opensearch_subnet_ids" {
  description = "Private subnet IDs for VPC placement. Leave empty to skip VPC placement (not recommended for prod)."
  type        = list(string)
  default     = []
}

variable "opensearch_security_group_ids" {
  description = "Security group IDs granting ingress to the OpenSearch domain."
  type        = list(string)
  default     = []
}

locals {
  opensearch_vpc_enabled = length(var.opensearch_subnet_ids) > 0
}

# Master user password generated and stashed in Secrets Manager — never in state.
resource "random_password" "opensearch_master" {
  length           = 24
  special          = true
  override_special = "-_.!@"
}

resource "aws_secretsmanager_secret" "opensearch_master" {
  name        = "${var.project_name}/opensearch/master"
  description = "Fine-grained access-control master user password for ${var.opensearch_domain_name}."

  tags = {
    Component = "storage"
    Layer     = "search"
  }
}

resource "aws_secretsmanager_secret_version" "opensearch_master" {
  secret_id     = aws_secretsmanager_secret.opensearch_master.id
  secret_string = jsonencode({
    username = var.opensearch_master_username
    password = random_password.opensearch_master.result
  })
}

resource "aws_opensearch_domain" "main" {
  domain_name    = var.opensearch_domain_name
  engine_version = var.opensearch_engine_version

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = var.opensearch_instance_count

    zone_awareness_enabled = var.opensearch_instance_count > 1
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.opensearch_volume_gb
  }

  encrypt_at_rest {
    enabled = true
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    internal_user_database_enabled = true
    anonymous_auth_enabled         = false

    master_user_options {
      master_user_name     = var.opensearch_master_username
      master_user_password = random_password.opensearch_master.result
    }
  }

  advanced_options = {
    "rest.action.multi.allow_explicit_index" = "true"
  }

  dynamic "vpc_options" {
    for_each = local.opensearch_vpc_enabled ? [1] : []
    content {
      subnet_ids         = var.opensearch_subnet_ids
      security_group_ids = var.opensearch_security_group_ids
    }
  }

  log_publishing_options {
    log_type                 = "ES_APPLICATION_LOGS"
    cloudwatch_log_group_arn = aws_cloudwatch_log_group.opensearch.arn
    enabled                  = true
  }

  tags = {
    Name      = var.opensearch_domain_name
    Component = "storage"
    Layer     = "search"
  }
}

resource "aws_cloudwatch_log_group" "opensearch" {
  name              = "/aws/opensearch/${var.opensearch_domain_name}"
  retention_in_days = 14

  tags = {
    Component = "storage"
    Layer     = "search"
  }
}

# Permissive resource policy; tighten with IAM principal ARNs in production.
resource "aws_opensearch_domain_policy" "main" {
  domain_name = aws_opensearch_domain.main.domain_name

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "*" }
      Action    = "es:*"
      Resource  = "${aws_opensearch_domain.main.arn}/*"
      Condition = {
        IpAddress = { "aws:SourceIp" = [var.vpc_cidr] }
      }
    }]
  })
}

output "opensearch_endpoint" {
  description = "HTTPS endpoint of the OpenSearch domain (feed into OPENSEARCH_URL)."
  value       = "https://${aws_opensearch_domain.main.endpoint}"
}

output "opensearch_domain_arn" {
  description = "ARN of the OpenSearch domain."
  value       = aws_opensearch_domain.main.arn
}

output "opensearch_master_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the master user credentials."
  value       = aws_secretsmanager_secret.opensearch_master.arn
}
