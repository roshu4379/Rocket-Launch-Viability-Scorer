data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.root}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

# 1. DynamoDB Table for Rolling Score History
resource "aws_dynamodb_table" "score_history" {
  name         = "${var.project}-score-history"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "launch_id"
  range_key    = "fetch_timestamp"

  attribute {
    name = "launch_id"
    type = "S"
  }

  attribute {
    name = "fetch_timestamp"
    type = "S"
  }
}

# 2. AWS Lambda Function
resource "aws_lambda_function" "api_handler" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.project}-handler"
  role             = var.lab_role_arn
  handler          = "lambda_function.handler"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime          = "python3.12"
  timeout          = 30

  environment {
    variables = {
      SECRET_NAME   = var.secret_name
      TABLE_NAME    = aws_dynamodb_table.score_history.name
      SNS_TOPIC_ARN = aws_sns_topic.scrub_alerts.arn
    }
  }
}

# 3. API Gateway HTTP API
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

# --- ALERTS FAN-OUT ARCHITECTURE ---

# 1. SNS Topic
resource "aws_sns_topic" "scrub_alerts" {
  name = "${var.project}-scrub-alerts"
}

# 2. SQS Queue (Consumer)
resource "aws_sqs_queue" "alert_queue" {
  name = "${var.project}-alert-queue"
}

# 3. Topic Subscription (Binds SNS to SQS)
resource "aws_sns_topic_subscription" "queue_target" {
  topic_arn = aws_sns_topic.scrub_alerts.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.alert_queue.arn
}

# 4. SQS Queue Policy (Allows SNS to publish to the Queue)
resource "aws_sqs_queue_policy" "sns_to_sqs" {
  queue_url = aws_sqs_queue.alert_queue.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "sns.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.alert_queue.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.scrub_alerts.arn
          }
        }
      }
    ]
  })
}



# Create the EventBridge (CloudWatch Events) Rule
# This defines the schedule (rate of 1 hour) to keep you under the 10,000 API call limit.
resource "aws_cloudwatch_event_rule" "hourly_trigger" {
  name                = "${var.project}-hourly-trigger"
  description         = "Triggers the Rocket Launch Viability Scorer Lambda every hour"
  schedule_expression = "rate(1 hour)"
}

# Set Your Lambda as the Target
# This links the rule created above to your specific Lambda function.
resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.hourly_trigger.name
  target_id = "TriggerLaunchScorerLambda"
  
  # Linked exactly to the Lambda resource name in your compute module
  arn       = aws_lambda_function.api_handler.arn 
}

# Grant Invocation Permissions
# Without this, EventBridge will trigger, but AWS IAM will block it from actually executing the Lambda.
resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  
  # Linked exactly to the Lambda resource name in your compute module
  function_name = aws_lambda_function.api_handler.function_name 
  
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.hourly_trigger.arn
}