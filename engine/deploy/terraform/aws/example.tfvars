# Example only: every id below is a placeholder.
region             = "us-east-1"
environment        = "staging"
vpc_id             = "vpc-0123456789abcdef0"
public_subnet_ids  = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0aaaaaaaaaaaaaaa2"]
private_subnet_ids = ["subnet-0bbbbbbbbbbbbbbb1", "subnet-0bbbbbbbbbbbbbbb2"]
allowed_cidrs      = ["203.0.113.0/24"]
certificate_arn    = "arn:aws:acm:us-east-1:111111111111:certificate/00000000-0000-0000-0000-000000000000"
image              = "ghcr.io/thedhruvhegde/scruple-engine@sha256:0000000000000000000000000000000000000000000000000000000000000000"
