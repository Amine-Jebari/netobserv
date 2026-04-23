# ═════════════════════════════════════════════════════════════════════════════
#  DynamoDB — AWS counterpart to the local DynamoDB Local container.
#
#  Two tables hold the live network topology built from BGP + flow signals:
#    • network-topology → one row per node (router, peer, endpoint)
#    • network-links    → one row per directed edge (source → target)
#
#  Both use PAY_PER_REQUEST billing so we pay only for actual traffic. Point-in-
#  time recovery is enabled to protect against bad writes from the topology
#  service.
# ═════════════════════════════════════════════════════════════════════════════

variable "dynamodb_topology_table_name" {
  description = "Table name for per-node topology records."
  type        = string
  default     = "network-topology"
}

variable "dynamodb_links_table_name" {
  description = "Table name for directed link records."
  type        = string
  default     = "network-links"
}

locals {
  dynamodb_common_tags = {
    Component = "storage"
    Layer     = "graph"
  }
}

resource "aws_dynamodb_table" "network_topology" {
  name         = "${var.project_name}-${var.dynamodb_topology_table_name}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "node_id"

  attribute {
    name = "node_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.dynamodb_common_tags, {
    Name = "${var.project_name}-${var.dynamodb_topology_table_name}"
    Role = "nodes"
  })
}

resource "aws_dynamodb_table" "network_links" {
  name         = "${var.project_name}-${var.dynamodb_links_table_name}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "source_node"
  range_key    = "target_node"

  attribute {
    name = "source_node"
    type = "S"
  }

  attribute {
    name = "target_node"
    type = "S"
  }

  # Secondary index lets us look up inbound edges for a given target.
  global_secondary_index {
    name            = "target-lookup"
    hash_key        = "target_node"
    range_key       = "source_node"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = merge(local.dynamodb_common_tags, {
    Name = "${var.project_name}-${var.dynamodb_links_table_name}"
    Role = "edges"
  })
}

output "dynamodb_table_names" {
  description = "Map of logical table name → actual DynamoDB table name."
  value = {
    topology = aws_dynamodb_table.network_topology.name
    links    = aws_dynamodb_table.network_links.name
  }
}

output "dynamodb_table_arns" {
  description = "Map of logical table name → DynamoDB table ARN."
  value = {
    topology = aws_dynamodb_table.network_topology.arn
    links    = aws_dynamodb_table.network_links.arn
  }
}
