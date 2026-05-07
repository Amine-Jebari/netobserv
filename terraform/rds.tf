# ═════════════════════════════════════════════════════════════════════════════
#  Amazon RDS for PostgreSQL - AWS counterpart to the local
#  pgvector/pgvector:pg16 container that backs the agent's RAG knowledge base.
#
#  Notes:
#  - pgvector is built into RDS PostgreSQL 15.2+ as a trusted extension; it is
#    enabled by `CREATE EXTENSION vector` after provisioning. The custom
#    parameter group below explicitly preloads it via shared_preload_libraries
#    so HNSW build operations have access from the moment the cluster boots.
#  - The instance is db.t3.micro by default (demo profile). Override the
#    `rds_instance_class` variable for production.
#  - Networking is intentionally permissive within the VPC only: the security
#    group allows TCP 5432 from the VPC CIDR. No ingress from the public
#    internet. Tighten further by replacing `var.vpc_cidr` with caller SGs.
# ═════════════════════════════════════════════════════════════════════════════

variable "rds_identifier" {
  description = "RDS DB instance identifier."
  type        = string
  default     = "netobserv-pgvector"
}

variable "rds_engine_version" {
  description = "PostgreSQL engine version. Must be >= 15.2 for built-in pgvector."
  type        = string
  default     = "16.3"
}

variable "rds_instance_class" {
  description = "RDS instance class. db.t3.micro is sufficient for the demo corpus."
  type        = string
  default     = "db.t3.micro"
}

variable "rds_allocated_storage_gb" {
  description = "Allocated storage in GiB for the DB instance."
  type        = number
  default     = 20
}

variable "rds_max_allocated_storage_gb" {
  description = "Upper bound for storage autoscaling (GiB)."
  type        = number
  default     = 100
}

variable "rds_database_name" {
  description = "Initial database name created inside the instance."
  type        = string
  default     = "netobserv"
}

variable "rds_master_username" {
  description = "Master username for the RDS instance."
  type        = string
  default     = "netobserv"
}

variable "rds_subnet_ids" {
  description = "Private subnet IDs for the DB subnet group (>=2 in different AZs)."
  type        = list(string)
  default     = []
}

variable "rds_vpc_id" {
  description = "VPC ID hosting the security group. Required when creating the SG."
  type        = string
  default     = ""
}

variable "rds_backup_retention_days" {
  description = "Number of days automated backups are retained. 0 disables backups."
  type        = number
  default     = 7
}

variable "rds_deletion_protection" {
  description = "Whether the instance is protected from accidental deletion."
  type        = bool
  default     = true
}

variable "rds_skip_final_snapshot" {
  description = "Whether `terraform destroy` skips the final snapshot. Keep false in production."
  type        = bool
  default     = false
}

variable "rds_publicly_accessible" {
  description = "If true, the instance has a public endpoint. Keep false unless explicitly needed."
  type        = bool
  default     = false
}

locals {
  rds_create_subnet_group = length(var.rds_subnet_ids) > 0
  rds_create_security_group = var.rds_vpc_id != ""
}

# ── Master password generated and stored in Secrets Manager ──────────────────
resource "random_password" "rds_master" {
  length           = 32
  special          = true
  override_special = "-_.!@"
}

resource "aws_secretsmanager_secret" "rds_master" {
  name        = "${var.project_name}/rds/master"
  description = "Master credentials for the ${var.rds_identifier} pgvector RDS instance."

  tags = {
    Component = "storage"
    Layer     = "vector-db"
  }
}

resource "aws_secretsmanager_secret_version" "rds_master" {
  secret_id = aws_secretsmanager_secret.rds_master.id
  secret_string = jsonencode({
    username = var.rds_master_username
    password = random_password.rds_master.result
    dbname   = var.rds_database_name
  })
}

# ── Subnet group ─────────────────────────────────────────────────────────────
resource "aws_db_subnet_group" "main" {
  count = local.rds_create_subnet_group ? 1 : 0

  name        = "${var.project_name}-rds"
  description = "Private subnets for the ${var.rds_identifier} pgvector instance."
  subnet_ids  = var.rds_subnet_ids

  tags = {
    Name      = "${var.project_name}-rds"
    Component = "storage"
    Layer     = "vector-db"
  }
}

# ── Security group: VPC-only TCP 5432 ────────────────────────────────────────
resource "aws_security_group" "rds" {
  count = local.rds_create_security_group ? 1 : 0

  name        = "${var.project_name}-rds"
  description = "Allow Postgres traffic from inside the VPC to the pgvector RDS instance."
  vpc_id      = var.rds_vpc_id

  ingress {
    description = "Postgres from VPC CIDR"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all outbound (RDS does not initiate outbound traffic in practice)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name      = "${var.project_name}-rds"
    Component = "storage"
    Layer     = "vector-db"
  }
}

# ── Custom parameter group preloading pgvector ───────────────────────────────
#
# RDS Postgres 15.2+ ships pgvector as a "trusted extension" (no superuser
# required to CREATE EXTENSION). We still preload the library via
# shared_preload_libraries because some pgvector index build paths reference
# the loaded library at startup and the optimisation hint shaves CREATE INDEX
# time on first run.
resource "aws_db_parameter_group" "pgvector" {
  name        = "${var.project_name}-pgvector"
  family      = "postgres16"
  description = "Custom Postgres 16 parameter group with pgvector preloaded."

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,vector"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  tags = {
    Component = "storage"
    Layer     = "vector-db"
  }
}

# ── DB instance ──────────────────────────────────────────────────────────────
resource "aws_db_instance" "pgvector" {
  identifier     = var.rds_identifier
  engine         = "postgres"
  engine_version = var.rds_engine_version
  instance_class = var.rds_instance_class

  allocated_storage     = var.rds_allocated_storage_gb
  max_allocated_storage = var.rds_max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.rds_database_name
  username = var.rds_master_username
  password = random_password.rds_master.result

  parameter_group_name   = aws_db_parameter_group.pgvector.name
  db_subnet_group_name   = local.rds_create_subnet_group ? aws_db_subnet_group.main[0].name : null
  vpc_security_group_ids = local.rds_create_security_group ? [aws_security_group.rds[0].id] : []

  publicly_accessible = var.rds_publicly_accessible
  multi_az            = false # demo profile; flip to true for production

  backup_retention_period = var.rds_backup_retention_days
  copy_tags_to_snapshot   = true
  deletion_protection     = var.rds_deletion_protection
  skip_final_snapshot     = var.rds_skip_final_snapshot
  final_snapshot_identifier = var.rds_skip_final_snapshot ? null : "${var.rds_identifier}-final-${formatdate("YYYYMMDD-hhmm", timestamp())}"

  performance_insights_enabled = true
  monitoring_interval          = 60
  monitoring_role_arn          = aws_iam_role.rds_monitoring.arn

  apply_immediately = false
  auto_minor_version_upgrade = true

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = {
    Name      = var.rds_identifier
    Component = "storage"
    Layer     = "vector-db"
    Purpose   = "rag-knowledge-base"
  }

  # Ignore the rotated password drift - rotation is managed via Secrets Manager.
  lifecycle {
    ignore_changes = [password, final_snapshot_identifier]
  }
}

# ── Enhanced monitoring IAM role ─────────────────────────────────────────────
resource "aws_iam_role" "rds_monitoring" {
  name = "${var.project_name}-rds-monitoring"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Component = "storage"
    Layer     = "vector-db"
  }
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ── Outputs ──────────────────────────────────────────────────────────────────
output "rds_endpoint" {
  description = "RDS DNS endpoint (host:port). Feed into POSTGRES_HOST/POSTGRES_PORT."
  value       = aws_db_instance.pgvector.endpoint
}

output "rds_address" {
  description = "RDS DNS hostname only (no port)."
  value       = aws_db_instance.pgvector.address
}

output "rds_port" {
  description = "Postgres TCP port."
  value       = aws_db_instance.pgvector.port
}

output "rds_database_name" {
  description = "Initial database name."
  value       = aws_db_instance.pgvector.db_name
}

output "rds_master_secret_arn" {
  description = "Secrets Manager ARN holding the master credentials."
  value       = aws_secretsmanager_secret.rds_master.arn
}

output "rds_security_group_id" {
  description = "Security group ID granting Postgres access (empty if SG was not created)."
  value       = local.rds_create_security_group ? aws_security_group.rds[0].id : ""
}

output "rds_pgvector_init_sql" {
  description = "Run this once after the DB is reachable to enable pgvector + the runbook_chunks table."
  value       = <<-SQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    -- Then apply docker/postgres/init.sql to create the runbook_chunks table.
  SQL
}
