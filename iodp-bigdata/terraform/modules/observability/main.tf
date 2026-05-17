# terraform/modules/observability/main.tf
# CloudWatch Dashboards、Alarms、SNS
#
# 方案 D 调整：
#   - 删 MSK Consumer Lag 告警（MSK 已被 Firehose 替代）
#   - 加 Firehose DeliveryToS3 失败告警（取代 MSK Lag）
#   - 保留 Glue Job 失败告警 + DQ 阈值告警 + Dashboard
#
# 设计：
#   - SNS Topic 统一收发告警邮件
#   - Firehose DeliveryToS3.Records 异常 → 告警
#   - Glue Job 失败告警：任意 Task 失败即触发
#   - CloudWatch Dashboard：Firehose 吞吐 + Glue Duration 一览

# ─── SNS Topic（告警通知）───
resource "aws_sns_topic" "alerts" {
  name = "iodp-data-alerts-${var.environment}"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ─── Firehose 投递失败告警（取代 MSK Consumer Lag）───
resource "aws_cloudwatch_metric_alarm" "firehose_delivery_failure" {
  for_each = toset(var.firehose_stream_names)

  alarm_name          = "iodp-firehose-${each.key}-delivery-failure-${var.environment}"
  alarm_description   = "Firehose ${each.key} S3 delivery failure detected"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DeliveryToS3.DataFreshness"
  namespace           = "AWS/Firehose"
  period              = 300
  statistic           = "Maximum"
  threshold           = 900   # 15 min — 投递延迟超 15 min 视为异常

  dimensions = {
    DeliveryStreamName = each.key
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
  tags          = var.tags
}

# ─── Glue Job 失败告警 ───
resource "aws_cloudwatch_metric_alarm" "glue_job_failure" {
  for_each = toset(var.glue_job_names)

  alarm_name          = "iodp-glue-failure-${each.key}"
  alarm_description   = "Glue job ${each.key} failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "glue.driver.aggregate.numFailedTasks"
  namespace           = "Glue"
  period              = 300
  statistic           = "Sum"
  threshold           = 1

  dimensions = {
    JobName  = each.key
    JobRunId = "ALL"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  tags          = var.tags
}

# ─── DQ 阈值突破告警 ───
resource "aws_cloudwatch_metric_alarm" "dq_threshold_breach" {
  alarm_name          = "iodp-dq-threshold-breach-${var.environment}"
  alarm_description   = "DQ failure threshold breached - check DynamoDB dq_reports table"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "DQThresholdBreach"
  namespace           = "IODP/DataQuality"
  period              = 300
  statistic           = "Sum"
  threshold           = 1

  alarm_actions = [aws_sns_topic.alerts.arn]
  tags          = var.tags
}

# ─── CloudWatch Dashboard ───
resource "aws_cloudwatch_dashboard" "iodp" {
  dashboard_name = "IODP-BigData-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title = "Firehose Ingestion (Records/min)"
          metrics = [
            for stream in var.firehose_stream_names : [
              "AWS/Firehose", "IncomingRecords",
              "DeliveryStreamName", stream
            ]
          ]
          period = 60
          stat   = "Sum"
          view   = "timeSeries"
          region = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title = "Glue Job Duration"
          metrics = [
            for job in var.glue_job_names : [
              "Glue", "glue.driver.ExecutorRunTime", "JobName", job
            ]
          ]
          period = 300
          view   = "timeSeries"
          region = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title = "DQ Threshold Breaches"
          metrics = [
            ["IODP/DataQuality", "DQThresholdBreach"]
          ]
          period = 300
          stat   = "Sum"
          view   = "timeSeries"
          region = var.aws_region
        }
      }
    ]
  })
}
