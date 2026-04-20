variable "aws_region" {
  description = "AWS region to deploy all resources into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short identifier prepended to every resource name (e.g. 'netobserv')."
  type        = string
  default     = "netobserv"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project_name))
    error_message = "project_name must be 3-21 lowercase alphanumeric characters or hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "Deployment environment name used in resource tags and naming."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "environment must be one of: development, staging, production."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC that hosts all private services."
  type        = string
  default     = "10.0.0.0/16"
}
