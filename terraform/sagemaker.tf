# ═════════════════════════════════════════════════════════════════════════════
#  SageMaker — AWS counterpart to the local PyOD ensemble.
#
#  Local environment runs the trained models in-process inside the anomaly
#  scoring engine. On AWS, we host the same bundle behind a SageMaker Serverless
#  Inference endpoint to avoid paying for an always-on instance during demos.
#
#  As of Phase 3.2 the inference container is the same image that runs the
#  local engine (services/anomaly/Dockerfile). The image is published to ECR
#  under ``${ecr_repository_name}:${ecr_image_tag}``; SageMaker pulls it via
#  the assume-role policy declared below. The trainer's bundle layout
#  (``baseline-latest.tar.gz`` containing one .joblib per model + metadata.json)
#  is what the engine module already knows how to load, so no separate
#  inference handler is needed.
#
#  Model artifacts (the .tar.gz produced by services/anomaly/baseline.py) are
#  read from the S3 archive bucket created in s3.tf.
# ═════════════════════════════════════════════════════════════════════════════

variable "sagemaker_enabled" {
  description = "Set to false to skip SageMaker provisioning (e.g. for tf-plan smoke tests before the inference image is built)."
  type        = bool
  default     = false
}

variable "ecr_repository_name" {
  description = "ECR repository hosting the netobserv-anomaly engine image. Created out-of-band (Phase 5 packaging)."
  type        = string
  default     = "netobserv-anomaly"
}

variable "ecr_image_tag" {
  description = "Image tag used for the SageMaker model container. Bump on each release."
  type        = string
  default     = "engine-v0.2.0"
}

variable "sagemaker_model_image_uri" {
  description = "Override-able full ECR image URI; defaults to the canonical netobserv-anomaly engine image in the deployer's account."
  type        = string
  default     = ""
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

data "aws_caller_identity" "current" {}

locals {
  sagemaker_common_tags = {
    Component = "anomaly"
    Layer     = "ml-inference"
  }

  # When sagemaker_enabled is false, count = 0 disables every resource cleanly.
  sagemaker_count = var.sagemaker_enabled ? 1 : 0

  # Resolved model image URI: override → explicit value, otherwise the canonical
  # ECR repo in the current account/region.
  sagemaker_model_image_uri_resolved = (
    var.sagemaker_model_image_uri != ""
    ? var.sagemaker_model_image_uri
    : "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.ecr_repository_name}:${var.ecr_image_tag}"
  )
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
    image          = local.sagemaker_model_image_uri_resolved
    model_data_url = "s3://${aws_s3_bucket.archive.bucket}/${var.sagemaker_model_artifact_key}"

    environment = {
      # The engine reads its bundle from S3 directly; SageMaker still mounts
      # the artifact at /opt/ml/model so we expose both for compatibility.
      MODEL_KEY           = var.sagemaker_model_artifact_key
      S3_BUCKET           = aws_s3_bucket.archive.bucket
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

output "sagemaker_model_image_uri" {
  description = "ECR image URI hosting the inference container. Useful for sanity-checking deploys."
  value       = local.sagemaker_model_image_uri_resolved
}
