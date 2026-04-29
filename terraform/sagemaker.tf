# ═════════════════════════════════════════════════════════════════════════════
#  SageMaker — AWS counterpart to the local PyOD ensemble.
#
#  Local environment runs the trained models in-process inside the anomaly
#  scoring engine. On AWS, we host the same bundle behind a SageMaker Serverless
#  Inference endpoint to avoid paying for an always-on instance during demos.
#
#  The model image URI is a placeholder — Phase 3.3 will containerize the
#  inference handler (Flask/FastAPI on top of the PyOD bundle), push it to ECR,
#  and replace ``var.sagemaker_model_image_uri`` with the real reference.
#
#  Model artifacts (the .tar.gz produced by services/anomaly/baseline.py) are
#  read from the S3 archive bucket created in s3.tf.
# ═════════════════════════════════════════════════════════════════════════════

variable "sagemaker_enabled" {
  description = "Set to false to skip SageMaker provisioning (e.g. for tf-plan smoke tests before the inference image is built)."
  type        = bool
  default     = false
}

variable "sagemaker_model_image_uri" {
  description = "ECR image URI for the inference container. Placeholder until Phase 3.3 builds the real image."
  type        = string
  default     = "placeholder.dkr.ecr.us-east-1.amazonaws.com/netobserv-anomaly:latest"
}

variable "sagemaker_model_artifact_key" {
  description = "S3 key of the model bundle. Defaults to the 'latest' alias the baseline trainer keeps current."
  type        = string
  default     = "models/baseline-latest.tar.gz"
}

variable "sagemaker_serverless_memory_mb" {
  description = "Serverless inference memory size (MB). Allowed values: 1024, 2048, 3072, 4096, 5120, 6144."
  type        = number
  default     = 2048

  validation {
    condition     = contains([1024, 2048, 3072, 4096, 5120, 6144], var.sagemaker_serverless_memory_mb)
    error_message = "memory_size_in_mb must be one of 1024, 2048, 3072, 4096, 5120, 6144."
  }
}

variable "sagemaker_serverless_max_concurrency" {
  description = "Maximum concurrent invocations for the serverless endpoint."
  type        = number
  default     = 5

  validation {
    condition     = var.sagemaker_serverless_max_concurrency >= 1 && var.sagemaker_serverless_max_concurrency <= 200
    error_message = "max_concurrency must be between 1 and 200."
  }
}

locals {
  sagemaker_common_tags = {
    Component = "anomaly"
    Layer     = "ml-inference"
  }

  # When sagemaker_enabled is false, count = 0 disables every resource cleanly.
  sagemaker_count = var.sagemaker_enabled ? 1 : 0
}

# ── IAM execution role ───────────────────────────────────────────────────────

data "aws_iam_policy_document" "sagemaker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sagemaker_execution" {
  count              = local.sagemaker_count
  name               = "${var.project_name}-sagemaker-execution"
  assume_role_policy = data.aws_iam_policy_document.sagemaker_assume.json

  tags = merge(local.sagemaker_common_tags, {
    Name = "${var.project_name}-sagemaker-execution"
  })
}

# Read model artifacts from the archive bucket created in s3.tf.
data "aws_iam_policy_document" "sagemaker_s3_read" {
  statement {
    sid     = "ReadModelArtifacts"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.archive.arn,
      "${aws_s3_bucket.archive.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "sagemaker_s3_read" {
  count  = local.sagemaker_count
  name   = "${var.project_name}-sagemaker-s3-read"
  role   = aws_iam_role.sagemaker_execution[0].id
  policy = data.aws_iam_policy_document.sagemaker_s3_read.json
}

# CloudWatch logs/metrics for endpoint observability.
resource "aws_iam_role_policy_attachment" "sagemaker_cloudwatch" {
  count      = local.sagemaker_count
  role       = aws_iam_role.sagemaker_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess"
}

# Allow the endpoint container to pull the inference image from ECR.
resource "aws_iam_role_policy_attachment" "sagemaker_ecr_read" {
  count      = local.sagemaker_count
  role       = aws_iam_role.sagemaker_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ── SageMaker model + endpoint config + endpoint ─────────────────────────────

resource "aws_sagemaker_model" "anomaly" {
  count              = local.sagemaker_count
  name               = "${var.project_name}-anomaly-model"
  execution_role_arn = aws_iam_role.sagemaker_execution[0].arn

  primary_container {
    image          = var.sagemaker_model_image_uri
    model_data_url = "s3://${aws_s3_bucket.archive.bucket}/${var.sagemaker_model_artifact_key}"

    environment = {
      MODEL_PREFIX        = "models"
      BUNDLE_FORMAT       = "joblib"
      LOG_LEVEL           = "INFO"
      AWS_DEFAULT_REGION  = var.aws_region
    }
  }

  tags = merge(local.sagemaker_common_tags, {
    Name = "${var.project_name}-anomaly-model"
  })
}

resource "aws_sagemaker_endpoint_configuration" "anomaly" {
  count = local.sagemaker_count
  name  = "${var.project_name}-anomaly-endpoint-config"

  production_variants {
    variant_name = "primary"
    model_name   = aws_sagemaker_model.anomaly[0].name

    serverless_config {
      memory_size_in_mb = var.sagemaker_serverless_memory_mb
      max_concurrency   = var.sagemaker_serverless_max_concurrency
    }
  }

  tags = merge(local.sagemaker_common_tags, {
    Name = "${var.project_name}-anomaly-endpoint-config"
  })
}

resource "aws_sagemaker_endpoint" "anomaly" {
  count                = local.sagemaker_count
  name                 = "${var.project_name}-anomaly-endpoint"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.anomaly[0].name

  tags = merge(local.sagemaker_common_tags, {
    Name = "${var.project_name}-anomaly-endpoint"
  })
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "sagemaker_endpoint_name" {
  description = "Name of the SageMaker Serverless endpoint hosting the anomaly bundle."
  value       = local.sagemaker_count > 0 ? aws_sagemaker_endpoint.anomaly[0].name : null
}

output "sagemaker_endpoint_arn" {
  description = "ARN of the SageMaker endpoint."
  value       = local.sagemaker_count > 0 ? aws_sagemaker_endpoint.anomaly[0].arn : null
}

output "sagemaker_execution_role_arn" {
  description = "ARN of the IAM role assumed by the SageMaker model container."
  value       = local.sagemaker_count > 0 ? aws_iam_role.sagemaker_execution[0].arn : null
}

output "sagemaker_model_artifact_uri" {
  description = "S3 URI of the model bundle the endpoint loads."
  value       = "s3://${aws_s3_bucket.archive.bucket}/${var.sagemaker_model_artifact_key}"
}
