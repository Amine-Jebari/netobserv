# ═════════════════════════════════════════════════════════════════════════════
#  Aggregate outputs consumed by the Phase 4.2 agent service.
#
#  The agent connects to four backends:
#    1. The topology service (HTTP REST + WebSocket).
#    2. InfluxDB / Timestream (time-series metrics).
#    3. OpenSearch (log + event search).
#    4. RDS Postgres + pgvector (RAG corpus + investigations table).
#
#  Per-resource outputs already live alongside their resources (see
#  rds.tf / opensearch.tf / s3.tf / etc.). This file does not redeclare them
#  — that would be a Terraform naming collision. Instead it adds:
#
#    • the agent-only outputs that don't have a natural home in another
#      resource file (topology_service_url, agent_env);
#    • a single aggregate `agent_service_config` output the deploy pipeline
#      can pass straight to the agent's task definition / Helm chart.
# ═════════════════════════════════════════════════════════════════════════════


# ── Topology service URL ─────────────────────────────────────────────────────
# The topology service is deployed in a future phase (ECS Fargate behind an
# internal ALB, mirroring the local docker-compose `topology` service). Until
# that resource exists, surface the URL as a variable so the agent can be
# pointed at any reachable endpoint (e.g. an existing internal ALB DNS name).

variable "topology_service_url" {
  description = "Base URL of the topology REST API (e.g. http://internal-alb.dns/). Override per environment until the topology Fargate service is provisioned."
  type        = string
  default     = ""
}

output "topology_service_url" {
  description = "URL the agent service should hit for /topology/node and /topology/affected. Empty if not yet deployed."
  value       = var.topology_service_url
}


# ── InfluxDB equivalent endpoint ─────────────────────────────────────────────
# The agent's get_metrics tool talks to InfluxDB locally. On AWS, the project
# uses Timestream — the agent will need a thin adapter that translates Flux
# queries into Timestream SQL. Until that adapter ships, we expose Timestream's
# write database name and a synthetic `influxdb_endpoint` derived from the
# region so deployment templates have a single value to bind.

output "influxdb_endpoint" {
  description = "Endpoint the agent's get_metrics tool reads from. On AWS this is Timestream's regional query endpoint; on the local stack it's the InfluxDB container."
  value       = "https://query-cell1.timestream.${var.aws_region}.amazonaws.com"
}

output "influxdb_database" {
  description = "Logical database/bucket name for time-series queries (Timestream database on AWS)."
  value       = aws_timestreamwrite_database.main.database_name
}


# ── Agent service config bundle ──────────────────────────────────────────────
# Single map containing every environment variable the agent service needs so
# downstream tooling (CDK consumer, Helm chart, ECS task def template) can
# read one output instead of nine.

output "agent_service_config" {
  description = "Environment variables for the agent service deployment."
  value = {
    TOPOLOGY_SERVICE_URL = var.topology_service_url
    INFLUXDB_URL         = "https://query-cell1.timestream.${var.aws_region}.amazonaws.com"
    INFLUXDB_BUCKET      = aws_timestreamwrite_database.main.database_name
    OPENSEARCH_URL       = "https://${aws_opensearch_domain.main.endpoint}"
    POSTGRES_HOST        = aws_db_instance.pgvector.address
    POSTGRES_PORT        = tostring(aws_db_instance.pgvector.port)
    POSTGRES_DB          = aws_db_instance.pgvector.db_name
    # Secret ARNs — the deployment is expected to wire these into the task
    # definition's `secrets` block rather than reading the plaintext values
    # at provision time.
    POSTGRES_SECRET_ARN  = aws_secretsmanager_secret.rds_master.arn
    OPENSEARCH_SECRET_ARN = aws_secretsmanager_secret.opensearch_master.arn
    # Kinesis equivalent of the Kafka anomalies.scored topic.
    KAFKA_TOPIC_ANOMALIES = aws_kinesis_stream.anomalies_scored.name
  }
  sensitive = false
}


# ── Convenience aliases for the four endpoints called out in Phase 4.2 ───────
# These re-expose individual values so consumers can pluck them by name; the
# underlying resources are owned by the corresponding *.tf files.

output "agent_topology_service_url" {
  description = "Alias for topology_service_url, scoped to the agent service."
  value       = var.topology_service_url
}

output "agent_rds_endpoint" {
  description = "Postgres endpoint the agent uses for RAG + investigations."
  value       = aws_db_instance.pgvector.endpoint
}

output "agent_opensearch_endpoint" {
  description = "OpenSearch endpoint the agent's get_recent_events tool queries."
  value       = "https://${aws_opensearch_domain.main.endpoint}"
}

output "agent_influxdb_endpoint" {
  description = "Timestream/InfluxDB endpoint the agent's get_metrics tool queries."
  value       = "https://query-cell1.timestream.${var.aws_region}.amazonaws.com"
}
