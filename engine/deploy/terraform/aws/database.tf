resource "aws_db_subnet_group" "this" {
  name       = local.prefix
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.prefix}-pg${var.db_engine_version}"
  family = "postgres${var.db_engine_version}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "this" {
  identifier     = local.prefix
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = local.db_name
  username = local.db_user
  # RDS creates and rotates the password in Secrets Manager; it never enters
  # Terraform state or variables.
  manage_master_user_password = true

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_allocated_storage * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true

  deletion_protection       = var.environment == "prod"
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.prefix}-final" : null

  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
}
