data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.root}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

resource "aws_lambda_function" "api_handler" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.project}-handler"
  role             = var.lab_role_arn
  handler          = "lambda_function.handler"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime          = "python3.12"

  environment {
    variables = {
      SECRET_NAME = var.secret_name
    }
  }
}

resource "aws_apigatewayv2_api" "http_api" {
  name          = "${var.project}-front-door"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id                 = aws_apigatewayv2_api.http_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api_handler.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "default_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "GET /score"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

resource "aws_apigatewayv2_stage" "default_stage" {
  api_id      = aws_apigatewayv2_api.http_api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*"
}