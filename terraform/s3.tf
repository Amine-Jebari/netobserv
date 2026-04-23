# ═════════════════════════════════════════════════════════════════════════════
#  S3 — AWS counterpart to the local MinIO archive bucket.
#
#  Versioned bucket with a lifecycle rule that moves cold objects to Glacier
#  after 30 days and expires them after 365 days. Enforces encryption at rest
#  and blocks all public access. The collector writes NDJSON archive objects
#  under the same key schema used by MinIO locally.
# ═════════════════════════════════════════════════════════════════════════════

variable "s3_archive_bucket_name" {
  description = "Archive bucket name. Must be globally unique; a random suffix is appended."
  type        = string
  default     = "netobserv-archive"
}

variable "s3_glacier_transition_days" {
  description = "Age at which objects transition to Glacier Deep Archive."
  type        = number
  default     = 30

  validation {
    condition     = var.s3_glacier_transition_days >= 1
    error_message = "Glacier transition must be at least 1 day."
  }
}

variable "s3_expiration_days" {
  description = "Age at which archive objects are permanently deleted."
  type        = number
  default     = 365

  validation {
    condition     = var.s3_expiration_days > var.s3_glacier_transition_days
    error_message = "Expiration must be greater than the Glacier transition age."
  }
}

resource "random_id" "archive_suffix" {
  byte_length = 3
}

resource "aws_s3_bucket" "archive" {
  bucket        = "${var.s3_archive_bucket_name}-${random_id.archive_suffix.hex}"
  force_destroy = false

  tags = {
    Name      = var.s3_archive_bucket_name
    Component = "storage"
    Layer     = "object"
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  # Depends on versioning — otherwise lifecycle rules collide with defaults.
  depends_on = [aws_s3_bucket_versioning.archive]

  rule {
    id     = "archive-tiering"
    status = "Enabled"

    filter {
      prefix = ""
    }

    transition {
      days          = var.s3_glacier_transition_days
      storage_class = "DEEP_ARCHIVE"
    }

    expiration {
      days = var.s3_expiration_days
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "DEEP_ARCHIVE"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

output "s3_archive_bucket" {
  description = "Archive bucket name (feed into S3_BUCKET)."
  value       = aws_s3_bucket.archive.bucket
}

output "s3_archive_bucket_arn" {
  description = "ARN of the archive bucket."
  value       = aws_s3_bucket.archive.arn
}
