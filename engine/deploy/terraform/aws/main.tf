locals {
  prefix   = "${var.name}-${var.environment}"
  app_port = 8000
  db_name  = "scruple"
  db_user  = "scruple"

  # Non-secret settings, identical for the service and the migration task.
  environment = [
    { name = "SCRUPLE_DATABASE_URL", value = "postgresql://${local.db_user}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${local.db_name}?sslmode=require" },
    { name = "SCRUPLE_API_HOST", value = "0.0.0.0" },
    { name = "SCRUPLE_API_PORT", value = tostring(local.app_port) },
    { name = "SCRUPLE_LOG_FORMAT", value = "json" },
    { name = "SCRUPLE_WEB_WORKERS", value = tostring(var.web_workers) },
    { name = "SCRUPLE_WEB_KEEPALIVE", value = "75" },
    { name = "SCRUPLE_MIGRATE_ON_START", value = "false" },
    { name = "PROMETHEUS_MULTIPROC_DIR", value = "/tmp/metrics" },
  ]

  # References only: ECS injects the values at task start. The RDS-managed
  # master secret is JSON; ":password::" selects one key.
  secrets = [
    { name = "SCRUPLE_DATABASE_PASSWORD", valueFrom = "${aws_db_instance.this.master_user_secret[0].secret_arn}:password::" },
    { name = "SCRUPLE_METRICS_TOKEN", valueFrom = aws_secretsmanager_secret.metrics_token.arn },
    { name = "SCRUPLE_HELDOUT_SALT", valueFrom = aws_secretsmanager_secret.heldout_salt.arn },
  ]

  security_context = {
    user                   = "10001:10001"
    readonlyRootFilesystem = true
    privileged             = false
    linuxParameters = {
      capabilities = { drop = ["ALL"] }
    }
    mountPoints = [
      { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
      { sourceVolume = "data", containerPath = "/data", readOnly = false },
    ]
  }

  repository_credentials = var.registry_credentials_secret_arn == null ? {} : {
    repositoryCredentials = { credentialsParameter = var.registry_credentials_secret_arn }
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/scruple/${var.environment}"
  retention_in_days = var.log_retention_days
}
