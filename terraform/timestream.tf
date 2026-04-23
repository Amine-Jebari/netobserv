# ═════════════════════════════════════════════════════════════════════════════
#  Amazon Timestream — AWS counterpart to the local InfluxDB 2.x instance.
#
#  Two tables mirror the two InfluxDB measurements written by the collector:
#    • metrics → SNMP device/interface metrics
#    • flows   → VPC flow record packet/byte counters (aggregated)
#
#  Retention is split: memory store keeps the hot window (fast writes, point
#  queries), magnetic store keeps the long tail (cheap scans, analytics).
# ═════════════════════════════════════════════════════════════════════════════

variable "timestream_db_name" {
  description = "Timestream database name. Uses the project prefix."
  type        = string
  default     = "netobserv"
}

variable "timestream_metrics_memory_hours" {
  description = "Hot window for SNMP metrics (hours held in memory store)."
  type        = number
  default     = 24

  validation {
    condition     = var.timestream_metrics_memory_hours >= 1 && var.timestream_metrics_memory_hours <= 8766
    error_message = "memory store retention must be between 1 and 8766 hours."
  }
}

variable "timestream_metrics_magnetic_days" {
  description = "Cold window for SNMP metrics (days held in magnetic store)."
  type        = number
  default     = 90

  validation {
    condition     = var.timestream_metrics_magnetic_days >= 1 && var.timestream_metrics_magnetic_days <= 73000
    error_message = "magnetic store retention must be between 1 and 73000 days."
  }
}

variable "timestream_flows_memory_hours" {
  description = "Hot window for flow aggregates (hours held in memory store)."
  type        = number
  default     = 12
}

variable "timestream_flows_magnetic_days" {
  description = "Cold window for flow aggregates (days held in magnetic store)."
  type        = number
  default     = 30
}

locals {
  timestream_common_tags = {
    Component = "storage"
    Layer     = "time-series"
  }
}

resource "aws_timestreamwrite_database" "main" {
  database_name = var.timestream_db_name

  tags = merge(local.timestream_common_tags, {
    Name = var.timestream_db_name
  })
}

resource "aws_timestreamwrite_table" "metrics" {
  database_name = aws_timestreamwrite_database.main.database_name
  table_name    = "metrics"

  retention_properties {
    memory_store_retention_period_in_hours  = var.timestream_metrics_memory_hours
    magnetic_store_retention_period_in_days = var.timestream_metrics_magnetic_days
  }

  magnetic_store_write_properties {
    enable_magnetic_store_writes = true
  }

  tags = merge(local.timestream_common_tags, {
    Name            = "metrics"
    SourceMeasurement = "snmp"
    LocalEquivalent = "influxdb://metrics/snmp"
  })
}

resource "aws_timestreamwrite_table" "flows" {
  database_name = aws_timestreamwrite_database.main.database_name
  table_name    = "flows"

  retention_properties {
    memory_store_retention_period_in_hours  = var.timestream_flows_memory_hours
    magnetic_store_retention_period_in_days = var.timestream_flows_magnetic_days
  }

  magnetic_store_write_properties {
    enable_magnetic_store_writes = true
  }

  tags = merge(local.timestream_common_tags, {
    Name              = "flows"
    SourceMeasurement = "flows"
    LocalEquivalent   = "influxdb://metrics/flows"
  })
}

output "timestream_database_name" {
  description = "Name of the Timestream database backing the collector."
  value       = aws_timestreamwrite_database.main.database_name
}

output "timestream_table_names" {
  description = "Map of logical table name → Timestream table name."
  value = {
    metrics = aws_timestreamwrite_table.metrics.table_name
    flows   = aws_timestreamwrite_table.flows.table_name
  }
}

output "timestream_table_arns" {
  description = "Map of logical table name → Timestream table ARN."
  value = {
    metrics = aws_timestreamwrite_table.metrics.arn
    flows   = aws_timestreamwrite_table.flows.arn
  }
}
