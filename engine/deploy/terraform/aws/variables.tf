variable "region" {
  description = "AWS region, e.g. us-east-1."
  type        = string
}

variable "environment" {
  description = "staging or prod. Used in names and tags."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "scruple"
}

variable "vpc_id" {
  description = "Existing VPC."
  type        = string
}

variable "public_subnet_ids" {
  description = "Subnets for the load balancer (two or more AZs)."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Subnets for the tasks and the database (two or more AZs, NAT egress)."
  type        = list(string)
}

variable "allowed_cidrs" {
  description = "Who may reach the load balancer. Default is nobody: set it on purpose."
  type        = list(string)
  default     = []
}

variable "certificate_arn" {
  description = "ACM certificate for the HTTPS listener."
  type        = string
}

variable "image" {
  description = "Image by digest, e.g. ghcr.io/thedhruvhegde/scruple-engine@sha256:..."
  type        = string
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "Deploy by digest (the release workflow prints it), not by tag."
  }
}

variable "registry_credentials_secret_arn" {
  description = "Secrets Manager secret with {username,password} for a private GHCR package. Null if public."
  type        = string
  default     = null
}

variable "desired_count" {
  type    = number
  default = 2
}

variable "task_cpu" {
  description = "Fargate CPU units (1024 = 1 vCPU)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate memory in MiB."
  type        = number
  default     = 1024
}

variable "web_workers" {
  description = "SCRUPLE_WEB_WORKERS. Keep 1 if you use the in-memory shadow endpoints."
  type        = number
  default     = 1
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage" {
  description = "GiB."
  type        = number
  default     = 50
}

variable "db_engine_version" {
  description = "PostgreSQL major version (the engine's CI tests 16)."
  type        = string
  default     = "16"
}

variable "db_multi_az" {
  type    = bool
  default = true
}

variable "db_backup_retention_days" {
  type    = number
  default = 14
}

variable "log_retention_days" {
  type    = number
  default = 90
}
