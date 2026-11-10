data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Used by the ECS agent: pull the image, read the referenced secrets, write logs.
resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = compact([
      aws_db_instance.this.master_user_secret[0].secret_arn,
      aws_secretsmanager_secret.metrics_token.arn,
      aws_secretsmanager_secret.heldout_salt.arn,
      var.registry_credentials_secret_arn,
    ])
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-scruple-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Used by the application itself. It calls no AWS API, so it has no policies.
resource "aws_iam_role" "task" {
  name               = "${local.prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
