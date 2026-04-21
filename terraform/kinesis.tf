# ═════════════════════════════════════════════════════════════════════════════
#  Kinesis Data Streams — AWS counterpart to the local Redpanda broker.
#
#  Four streams mirror the Kafka topics used by the local stack. On AWS,
#  the simulator is replaced by real VPC Flow Logs (via Kinesis Data Firehose)
#  feeding `network-flows`, so there is no Terraform file for the simulator
#  itself; only the ingestion plumbing is provisioned here.
#
#  Topic → stream name mapping:
#    network.flows      → <project>-network-flows
#    network.bgp        → <project>-network-bgp
#    network.snmp       → <project>-network-snmp
#    anomalies.scored   → <project>-anomalies-scored
# ═════════════════════════════════════════════════════════════════════════════

variable "kinesis_shard_count_flows" {
  description = "Shard count for the network flow stream (highest volume)."
  type        = number
  default     = 4

  validation {
    condition     = var.kinesis_shard_count_flows >= 1 && var.kinesis_shard_count_flows <= 1000
    error_message = "shard count must be between 1 and 1000."
  }
}

variable "kinesis_shard_count_bgp" {
  description = "Shard count for the BGP event stream (low volume)."
  type        = number
  default     = 1
}

variable "kinesis_shard_count_snmp" {
  description = "Shard count for the SNMP metric stream (medium volume)."
  type        = number
  default     = 2
}

variable "kinesis_shard_count_anomalies" {
  description = "Shard count for the scored-anomaly stream (low volume, fan-out)."
  type        = number
  default     = 1
}

variable "kinesis_retention_hours" {
  description = "Data retention window for all Kinesis streams (24-8760 hours)."
  type        = number
  default     = 48

  validation {
    condition     = var.kinesis_retention_hours >= 24 && var.kinesis_retention_hours <= 8760
    error_message = "retention must be between 24 and 8760 hours."
  }
}

locals {
  kinesis_common_tags = {
    Component = "ingestion"
    Layer     = "streaming"
  }
}

resource "aws_kinesis_stream" "network_flows" {
  name             = "${var.project_name}-network-flows"
  shard_count      = var.kinesis_shard_count_flows
  retention_period = var.kinesis_retention_hours

  shard_level_metrics = [
    "IncomingBytes",
    "IncomingRecords",
    "OutgoingBytes",
    "OutgoingRecords",
    "IteratorAgeMilliseconds",
  ]

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  tags = merge(local.kinesis_common_tags, {
    Name        = "${var.project_name}-network-flows"
    StreamType  = "vpc-flow-logs"
    LocalTopic  = "network.flows"
  })
}

resource "aws_kinesis_stream" "network_bgp" {
  name             = "${var.project_name}-network-bgp"
  shard_count      = var.kinesis_shard_count_bgp
  retention_period = var.kinesis_retention_hours

  shard_level_metrics = [
    "IncomingRecords",
    "OutgoingRecords",
    "IteratorAgeMilliseconds",
  ]

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  tags = merge(local.kinesis_common_tags, {
    Name       = "${var.project_name}-network-bgp"
    StreamType = "bgp-events"
    LocalTopic = "network.bgp"
  })
}

resource "aws_kinesis_stream" "network_snmp" {
  name             = "${var.project_name}-network-snmp"
  shard_count      = var.kinesis_shard_count_snmp
  retention_period = var.kinesis_retention_hours

  shard_level_metrics = [
    "IncomingRecords",
    "OutgoingRecords",
    "IteratorAgeMilliseconds",
  ]

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  tags = merge(local.kinesis_common_tags, {
    Name       = "${var.project_name}-network-snmp"
    StreamType = "snmp-metrics"
    LocalTopic = "network.snmp"
  })
}

resource "aws_kinesis_stream" "anomalies_scored" {
  name             = "${var.project_name}-anomalies-scored"
  shard_count      = var.kinesis_shard_count_anomalies
  retention_period = var.kinesis_retention_hours

  shard_level_metrics = [
    "IncomingRecords",
    "OutgoingRecords",
    "IteratorAgeMilliseconds",
  ]

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  tags = merge(local.kinesis_common_tags, {
    Name       = "${var.project_name}-anomalies-scored"
    StreamType = "anomaly-scores"
    LocalTopic = "anomalies.scored"
  })
}

output "kinesis_stream_arns" {
  description = "ARNs of every Kinesis stream, keyed by logical name."
  value = {
    network_flows    = aws_kinesis_stream.network_flows.arn
    network_bgp      = aws_kinesis_stream.network_bgp.arn
    network_snmp     = aws_kinesis_stream.network_snmp.arn
    anomalies_scored = aws_kinesis_stream.anomalies_scored.arn
  }
}

output "kinesis_stream_names" {
  description = "Names of every Kinesis stream, keyed by logical name."
  value = {
    network_flows    = aws_kinesis_stream.network_flows.name
    network_bgp      = aws_kinesis_stream.network_bgp.name
    network_snmp     = aws_kinesis_stream.network_snmp.name
    anomalies_scored = aws_kinesis_stream.anomalies_scored.name
  }
}
