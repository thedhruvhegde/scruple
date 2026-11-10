# Containers only. Put the values in out of band, e.g.
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string "$(openssl rand -hex 32)"
# Terraform never sees them, so they are not in state.

resource "aws_secretsmanager_secret" "metrics_token" {
  name                    = "${local.prefix}/metrics-token"
  description             = "Bearer token for GET /metrics (SCRUPLE_METRICS_TOKEN)."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "heldout_salt" {
  name                    = "${local.prefix}/heldout-salt"
  description             = "Seals the held-out seed namespace (SCRUPLE_HELDOUT_SALT)."
  recovery_window_in_days = 30
}
