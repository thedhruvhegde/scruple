# Scruple engine, single-tenant on AWS: ECS Fargate + ALB + RDS PostgreSQL +
# Secrets Manager.
#
# UNTESTED AGAINST A REAL AWS ACCOUNT. This is a skeleton: `terraform validate`
# passes, nothing here has been planned or applied against AWS. Review every
# resource, and run a plan in a sandbox account, before using it.
#
# Inputs it expects to exist already: a VPC with public and private subnets (NAT
# for egress), an ACM certificate for the API hostname. See docs/DEPLOY.md.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      app         = "scruple"
      environment = var.environment
      managed-by  = "terraform"
    }
  }
}
