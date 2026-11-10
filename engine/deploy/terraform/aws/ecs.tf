resource "aws_ecs_cluster" "this" {
  name = local.prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  volume {
    name = "tmp"
  }

  volume {
    name = "data"
  }

  container_definitions = jsonencode([merge(local.security_context, local.repository_credentials, {
    name         = "api"
    image        = var.image
    essential    = true
    portMappings = [{ containerPort = local.app_port, protocol = "tcp" }]
    environment  = local.environment
    secrets      = local.secrets
    stopTimeout  = 40
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
      interval    = 10
      timeout     = 3
      retries     = 3
      startPeriod = 20
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  })])
}

# One-off: alembic upgrade head, then the idempotent baseline import. Run it
# before each deploy (docs/DEPLOY.md, "Migrations"):
#   aws ecs run-task --cluster <cluster> --task-definition <migrate family> --launch-type FARGATE \
#     --network-configuration 'awsvpcConfiguration={subnets=[...],securityGroups=[<app sg>]}'
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  volume {
    name = "tmp"
  }

  volume {
    name = "data"
  }

  container_definitions = jsonencode([merge(local.security_context, local.repository_credentials, {
    name        = "migrate"
    image       = var.image
    essential   = true
    entryPoint  = ["/usr/bin/tini", "--", "sh", "-c"]
    command     = ["python -m scruple.storage.migrate && scruple rules > /dev/null"]
    environment = local.environment
    secrets     = local.secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migrate"
      }
    }
  })])
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.this.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = local.app_port
  }

  depends_on = [aws_lb_listener.https]
}
