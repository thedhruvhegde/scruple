output "url" {
  description = "Point a DNS name at this, matching the certificate."
  value       = "https://${aws_lb.this.dns_name}"
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "migrate_task_definition" {
  description = "Run before every deploy (docs/DEPLOY.md)."
  value       = aws_ecs_task_definition.migrate.family
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "db_endpoint" {
  value = aws_db_instance.this.endpoint
}

output "db_master_secret_arn" {
  description = "RDS-managed; rotate with aws secretsmanager rotate-secret."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "metrics_token_secret_arn" {
  value = aws_secretsmanager_secret.metrics_token.arn
}

output "heldout_salt_secret_arn" {
  value = aws_secretsmanager_secret.heldout_salt.arn
}
