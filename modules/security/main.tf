resource "aws_secretsmanager_secret" "api_config" {
  name = "${var.project}-api-config-v2"
}

resource "aws_secretsmanager_secret_version" "api_config_val" {
  secret_id     = aws_secretsmanager_secret.api_config.id
  secret_string = "dummy-ll2-key-for-now"
}