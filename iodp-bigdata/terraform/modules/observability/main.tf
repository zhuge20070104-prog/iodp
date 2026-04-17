# terraform/modules/observability/main.tf
# CloudWatch Dashboards、Alarms、SNS
#
# 设计：
#   - SNS Topic 统一收发告警邮件
#   - MSK Consumer Lag 告警：积压超 5 分钟触发
#   - Glue Job 失败告警：任意 Task 失败即触发
#   - CloudWatch Dashboard：MSK Lag + Glue Duration 一览

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

# ─── MSK Consumer Lag 告警 ───
resource "aws_cloudwatch_metric_alarm" "msk_consumer_lag" {
  for_each = toset(["user_clickstream", "system_app_logs"])

  alarm_name          = "iodp-msk-${each.key}-lag-${var.environment}"
  alarm_description   = "MSK consumer lag for ${each.key} exceeds threshold"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "EstimatedMaxTimeLag"
  namespace           = "AWS/Kafka"
  period              = 300   # 5 分钟
  statistic           = "Maximum"
  threshold           = 300   # 超过 5 分钟积压则告警

  dimensions = {
    Cluster = var.msk_cluster_name
    Topic   = each.key
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

# ─── DQ 阈值突破告警（DynamoDB Stream → CloudWatch Custom Metric 的补充）───
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
          title = "MSK Consumer Lag"
          metrics = [
            for topic in ["user_clickstream", "system_app_logs"] : [
              "AWS/Kafka", "EstimatedMaxTimeLag",
              "Cluster", var.msk_cluster_name, "Topic", topic
            ]
          ]
          period = 300
          view   = "timeSeries"
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
        }
      }
    ]
  })
}
