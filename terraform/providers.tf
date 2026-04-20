terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Uncomment and configure when you have an S3 bucket for remote state:
  # backend "s3" {
  #   bucket         = "netobserv-terraform-state"
  #   key            = "global/s3/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "netobserv-terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
