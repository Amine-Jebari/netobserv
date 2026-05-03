# ═════════════════════════════════════════════════════════════════════════════
#  Lambda — async wrapper around the SageMaker anomaly endpoint.
#
#  The local engine (services/anomaly/engine.py) consumes Kafka directly and
#  scores in-process. On AWS the equivalent is a Lambda function triggered by
#  the three telemetry Kinesis streams: each batch of records is forwarded to
#  the SageMaker Serverless endpoint (declared in sagemaker.tf) for scoring,
#  and any resulting anomaly events are written to the anomalies-scored stream.
#
#  Why Lambda over a long-running ECS task:
#    * Kinesis fan-out + back-pressure handling is built-in.
#    * Serverless inference + Lambda matches the demo budget (no idle cost).
#    * Cold-start is acceptable for a demo workload; the InvocationType=
#      RequestResponse path keeps end-to-end latency under a second per batch.
#
#  Function code is shipped as a zip under ``var.lambda_artifact_s3_key`` —
#  built and uploaded by the same CI pipeline that pushes the engine ECR image.
#  Until that pipeline exists, ``lambda_enabled = false`` keeps the resource
#  out of `terraform plan` output.
# ═════════════════════════════════════════════════════════════════════════════

variable "lambda_enabled" {
  description = "Set to false to skip Lambda provisioning (e.g. before the deployment artifact has been uploaded)."
  type        = bool
  default     = false
}

variable "lambda_function_name" {
  description = "Name of the anomaly scoring Lambda function."
  type        = string
  default     = "netobserv-anomaly-scorer"
}

variable "lambda_artifact_s3_key" {
  description = "S3 key (in the archive bucket) holding the deployment zip for the Lambda function."
  type        = string
  default     = "lambda/netobserv-anomaly-scorer.zip"
}

variable "lambda_runtime" {
  description = "Lambda runtime. Matches the engine's local Python version."
  type        = string
  default     = "python3.11"
}

variable "lambda_handler" {
  description = "Python module + function name the runtime invokes."
  type        = string
  default     = "handler.lambda_handler"
}

variable "lambda_memory_mb" {
  description = "Lambda memory size. PyOD scoring is CPU-bound, so this also determines vCPU share."
  type        = number
  default     = 1024

  validation {
    condition     = var.lambda_memory_mb >= 256 && var.lambda_memory_mb <= 10240
    error_message = "memory_mb must be between 256 and 10240."
  }
}

variable "lambda_timeout_seconds" {
  description = "Per-invocation timeout. Must exceed the longest expected SageMaker round-trip + Kinesis publish."
  type        = number
  default     = 60
}

variable "lambda_kinesis_batch_size" {
  description = "Records per Kinesis → Lambda batch."
  type        = number
  default     = 100
}

variable "lambda_kinesis_max_batching_window" {
  description = "Max seconds to wait for a partial batch to fill."
  type        = number
  default     = 5
}

locals {
  lambda_count = var.lambda_enabled ? 1 : 0

  lambda_common_tags = {
    Component = "anomaly"
    Layer     = "ml-inference-async"
  }

  # Source streams the Lambda subscribes to. The fourth stream
  # (anomalies-scored) is the *output* destination, not a trigger.
  lambda_source_stream_arns = [
    aws_kinesis_stream.network_flows.arn,
    aws_kinesis_stream.network_bgp.arn,
    aws_kinesis_stream.network_snmp.arn,
  ]
}

# ── IAM execution role ───────────────────────────────────────────────────────

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_anomaly" {
  count              = local.lambda_count
  name               = "${var.project_name}-lambda-anomaly"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = merge(local.lambda_common_tags, {
    Name = "${var.project_name}-lambda-anomaly"
  })
}

# CloudWatch logs for the function itself.
resource "aws_iam_role_policy_attachment" "lambda_basic_logs" {
  count      = local.lambda_count
  role       = aws_iam_role.lambda_anomaly[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Read from the source streams.
data "aws_iam_policy_document" "lambda_kinesis_read" {
  statement {
    sid    = "ConsumeTelemetryStreams"
    actions = [
      "kinesis:GetRecords",
      "kinesis:GetShardIterator",
      "kinesis:DescribeStream",
      "kinesis:ListShards",
      "kinesis:ListStreams",
    ]
    resources = local.lambda_source_stream_arns
  }
}

resource "aws_iam_role_policy" "lambda_kinesis_read" {
  count  = local.lambda_count
  name   = "${var.project_name}-lambda-kinesis-read"
  role   = aws_iam_role.lambda_anomaly[0].id
  policy = data.aws_iam_policy_document.lambda_kinesis_read.json
}

# Publish enriched events to the anomalies stream.
data "aws_iam_policy_document" "lambda_kinesis_write" {
  statement {
    sid       = "PublishAnomalyEvents"
    actions   = ["kinesis:PutRecord", "kinesis:PutRecords"]
    resources = [aws_kinesis_stream.anomalies_scored.arn]
  }
}

resource "aws_iam_role_policy" "lambda_kinesis_write" {
  count  = local.lambda_count
  name   = "${var.project_name}-lambda-kinesis-write"
  role   = aws_iam_role.lambda_anomaly[0].id
  policy = data.aws_iam_policy_document.lambda_kinesis_write.json
}

# Invoke the SageMaker endpoint declared in sagemaker.tf.
data "aws_iam_policy_document" "lambda_sagemaker_invoke" {
  count = (local.lambda_count > 0 && local.sagemaker_count > 0) ? 1 : 0

  statement {
    sid       = "InvokeAnomalyEndpoint"
    actions   = ["sagemaker:InvokeEndpoint"]
    resources = [aws_sagemaker_endpoint.anomaly[0].arn]
  }
}

resource "aws_iam_role_policy" "lambda_sagemaker_invoke" {
  count  = (local.lambda_count > 0 && local.sagemaker_count > 0) ? 1 : 0
  name   = "${var.project_name}-lambda-sagemaker-invoke"
  role   = aws_iam_role.lambda_anomaly[0].id
  policy = data.aws_iam_policy_document.lambda_sagemaker_invoke[0].json
}

# Read the deployment artifact from the archive bucket.
data "aws_iam_policy_document" "lambda_artifact_read" {
  statement {
    sid       = "ReadDeploymentArtifact"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.archive.arn}/${var.lambda_artifact_s3_key}"]
  }
}

resource "aws_iam_role_policy" "lambda_artifact_read" {
  count  = local.lambda_count
  name   = "${var.project_name}-lambda-artifact-read"
  role   = aws_iam_role.lambda_anomaly[0].id
  policy = data.aws_iam_policy_document.lambda_artifact_read.json
}

# ── Function ─────────────────────────────────────────────────────────────────

resource "aws_lambda_function" "anomaly_scorer" {
  count         = local.lambda_count
  function_name = var.lambda_function_name
  role          = aws_iam_role.lambda_anomaly[0].arn
  runtime       = var.lambda_runtime
  handler       = var.lambda_handler
  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb

  s3_bucket = aws_s3_bucket.archive.bucket
  s3_key    = var.lambda_artifact_s3_key

  environment {
    variables = {
      SAGEMAKER_ENDPOINT_NAME    = local.sagemaker_count > 0 ? aws_sagemaker_endpoint.anomaly[0].name : ""
      ANOMALIES_STREAM_NAME      = aws_kinesis_stream.anomalies_scored.name
      INFO_THRESHOLD             = "0.3"
      WARN_THRESHOLD             = "0.6"
      CRITICAL_THRESHOLD         = "0.85"
      LOG_LEVEL                  = "INFO"
    }
  }

  tags = merge(local.lambda_common_tags, {
    Name = var.lambda_function_name
  })
}

# ── Kinesis → Lambda triggers ────────────────────────────────────────────────

resource "aws_lambda_event_source_mapping" "flows" {
  count             = local.lambda_count
  event_source_arn  = aws_kinesis_stream.network_flows.arn
  function_name     = aws_lambda_function.anomaly_scorer[0].arn
  starting_position = "LATEST"
  batch_size                         = var.lambda_kinesis_batch_size
  maximum_batching_window_in_seconds = var.lambda_kinesis_max_batching_window
}

resource "aws_lambda_event_source_mapping" "bgp" {
  count             = local.lambda_count
  event_source_arn  = aws_kinesis_stream.network_bgp.arn
  function_name     = aws_lambda_function.anomaly_scorer[0].arn
  starting_position = "LATEST"
  batch_size                         = var.lambda_kinesis_batch_size
  maximum_batching_window_in_seconds = var.lambda_kinesis_max_batching_window
}

resource "aws_lambda_event_source_mapping" "snmp" {
  count             = local.lambda_count
  event_source_arn  = aws_kinesis_stream.network_snmp.arn
  function_name     = aws_lambda_function.anomaly_scorer[0].arn
  starting_position = "LATEST"
  batch_size                         = var.lambda_kinesis_batch_size
  maximum_batching_window_in_seconds = var.lambda_kinesis_max_batching_window
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "lambda_anomaly_function_arn" {
  description = "ARN of the anomaly scorer Lambda function."
  value       = local.lambda_count > 0 ? aws_lambda_function.anomaly_scorer[0].arn : null
}

output "lambda_anomaly_function_name" {
  description = "Name of the anomaly scorer Lambda function."
  value       = local.lambda_count > 0 ? aws_lambda_function.anomaly_scorer[0].function_name : null
}

output "lambda_anomaly_role_arn" {
  description = "Execution role ARN attached to the Lambda function."
  value       = local.lambda_count > 0 ? aws_iam_role.lambda_anomaly[0].arn : null
}
