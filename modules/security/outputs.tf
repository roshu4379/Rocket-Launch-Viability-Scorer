output "secret_name" {
  value = aws_secretsmanager_secret.api_config.name
}