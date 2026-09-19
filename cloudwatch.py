import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------
# AWS Clients
# ---------------------------------------------------------

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")
bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1"
)
logs_client = boto3.client("logs")


# ---------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------

EC2_INSTANCE_ID = os.environ["EC2_INSTANCE_ID"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]

DYNAMODB_TABLE = os.environ.get(
    "DYNAMODB_TABLE",
    "aiops-incidents"
)

SERVICE_NAME = os.environ.get(
    "SERVICE_NAME",
    "aiops-app"
)

CLOUDWATCH_LOG_GROUP = os.environ.get(
    "CLOUDWATCH_LOG_GROUP",
    "/aiops/application"
)


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

CONFIDENCE_THRESHOLD = 0.80
MAX_RETRIES = 2
WAIT_SECONDS = 5

table = dynamodb.Table(DYNAMODB_TABLE)


# ---------------------------------------------------------
# SNS Alert
# ---------------------------------------------------------

def send_sns_alert(
    incident_id,
    alarm_name,
    alarm_reason,
    analysis,
    remediation_status
):
    try:
        message = f"""
AIOps Incident Escalation

Incident ID: {incident_id}
Alarm: {alarm_name}
EC2 Instance: {EC2_INSTANCE_ID}
Service: {SERVICE_NAME}

Alarm Reason:
{alarm_reason}

Remediation Status:
{remediation_status}

AI Severity:
{analysis.get("severity", "UNKNOWN")}

AI Root Cause:
{analysis.get("root_cause", "UNKNOWN")}

AI Recommended Action:
{analysis.get("recommended_action", "UNKNOWN")}

AI Confidence:
{analysis.get("confidence", 0)}

AI Reason:
{analysis.get("reason", "UNKNOWN")}

Time:
{datetime.now(timezone.utc).isoformat()}
"""

        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="AIOps Incident Escalation",
            Message=message
        )

        logger.info(
            "SNS alert sent successfully"
        )

    except Exception:
        logger.exception(
            "Failed to send SNS alert"
        )


# ---------------------------------------------------------
# Get Recent Application Logs
# ---------------------------------------------------------

def get_recent_application_logs():
    try:
        response = logs_client.filter_log_events(
            logGroupName=CLOUDWATCH_LOG_GROUP,
            limit=30,
            interleaved=True
        )

        events = response.get(
            "events",
            []
        )

        if not events:
            return "No recent application logs found."

        log_messages = []

        for event in events:
            message = event.get(
                "message",
                ""
            )

            if message:
                log_messages.append(message)

        if not log_messages:
            return "No recent application logs found."

        logs_text = "\n".join(log_messages)

        return logs_text[-6000:]

    except Exception as exc:
        logger.warning(
            "Unable to retrieve application logs: %s",
            str(exc)
        )

        return (
            "Application logs unavailable."
        )


# ---------------------------------------------------------
# Parse Bedrock Response
# ---------------------------------------------------------

def parse_bedrock_response(text):
    if not text:
        raise ValueError(
            "Bedrock returned an empty response"
        )

    text = text.strip()

    logger.info(
        "Bedrock response before parsing: %s",
        text
    )

    # Remove Markdown code fences
    if text.startswith("```"):
        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

    # Additional cleanup for ```json and ```
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    text = text.strip()

    # Try direct JSON parsing
    try:
        return json.loads(text)

    except json.JSONDecodeError:
        logger.warning(
            "Direct JSON parsing failed. "
            "Trying JSON extraction."
        )

    # Extract JSON object from surrounding text
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "No valid JSON object found in Bedrock response"
        )

    json_text = text[start:end + 1]

    try:
        return json.loads(json_text)

    except json.JSONDecodeError:
        logger.exception(
            "Failed to parse extracted Bedrock JSON"
        )
        raise


# ---------------------------------------------------------
# Analyze Incident With Bedrock
# ---------------------------------------------------------

def analyze_incident_with_bedrock(
    alarm_name,
    alarm_reason
):
    application_logs = get_recent_application_logs()

    prompt = f"""
You are an AWS SRE AIOps engine.

Analyze the following production incident.

Alarm Name:
{alarm_name}

Alarm Reason:
{alarm_reason}

EC2 Instance:
{EC2_INSTANCE_ID}

Service:
{SERVICE_NAME}

Recent Application Logs:
{application_logs}

Choose exactly one recommended action from:

restart_service
restart_instance
no_action
escalate

Safety requirements:

- restart_service is allowed for a service-level issue.
- restart_instance should normally require manual approval.
- Use escalate when confidence is low.
- Confidence must be between 0 and 1.

Return ONLY valid JSON.

Do NOT use Markdown.
Do NOT use ```json.
Do NOT use ``` code fences.
Do NOT add any explanation outside the JSON.

Required JSON format:

{{
    "severity": "LOW|MEDIUM|HIGH|CRITICAL",
    "root_cause": "string",
    "recommended_action": "restart_service|restart_instance|no_action|escalate",
    "confidence": 0.0,
    "reason": "string"
}}
"""

    try:
        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            inferenceConfig={
                "maxTokens": 300,
                "temperature": 0
            }
        )

        text = response[
            "output"
        ][
            "message"
        ][
            "content"
        ][0]["text"]

        logger.info(
            "Raw Bedrock response: %s",
            text
        )

        analysis = parse_bedrock_response(
            text
        )

        # -------------------------------------------------
        # Validate Required Fields
        # -------------------------------------------------

        required_fields = [
            "severity",
            "root_cause",
            "recommended_action",
            "confidence",
            "reason"
        ]

        for field in required_fields:
            if field not in analysis:
                raise ValueError(
                    f"Missing field from AI response: {field}"
                )

        # -------------------------------------------------
        # Validate Action
        # -------------------------------------------------

        allowed_actions = {
            "restart_service",
            "restart_instance",
            "no_action",
            "escalate"
        }

        recommended_action = analysis[
            "recommended_action"
        ]

        if recommended_action not in allowed_actions:
            raise ValueError(
                f"Invalid recommended action: "
                f"{recommended_action}"
            )

        # -------------------------------------------------
        # Validate Confidence
        # -------------------------------------------------

        confidence = float(
            analysis["confidence"]
        )

        if confidence < 0 or confidence > 1:
            raise ValueError(
                "Confidence must be between 0 and 1"
            )

        analysis["confidence"] = confidence

        return analysis

    except Exception:
        logger.exception(
            "Bedrock incident analysis failed"
        )

        return {
            "severity": "HIGH",
            "root_cause": "AI analysis unavailable",
            "recommended_action": "escalate",
            "confidence": 0.0,
            "reason": "Bedrock analysis failed"
        }


# ---------------------------------------------------------
# Send SSM Command
# ---------------------------------------------------------

def send_ssm_command(
    commands
):
    try:
        response = ssm.send_command(
            InstanceIds=[
                EC2_INSTANCE_ID
            ],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": commands
            },
            TimeoutSeconds=60
        )

        command_id = response[
            "Command"
        ]["CommandId"]

        logger.info(
            "SSM command sent: %s",
            command_id
        )

        return command_id

    except Exception:
        logger.exception(
            "Failed to send SSM command"
        )
        return None


# ---------------------------------------------------------
# Get SSM Result
# ---------------------------------------------------------

def get_ssm_result(
    command_id
):
    try:
        response = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=EC2_INSTANCE_ID
        )

        status = response.get(
            "Status",
            "Unknown"
        )

        stdout = response.get(
            "StandardOutputContent",
            ""
        )

        stderr = response.get(
            "StandardErrorContent",
            ""
        )

        return {
            "status": status,
            "stdout": stdout,
            "stderr": stderr
        }

    except ClientError as exc:
        logger.warning(
            "Unable to get SSM invocation: %s",
            str(exc)
        )

        return {
            "status": "Unknown",
            "stdout": "",
            "stderr": str(exc)
        }

    except Exception:
        logger.exception(
            "Unexpected error getting SSM result"
        )

        return {
            "status": "Unknown",
            "stdout": "",
            "stderr": "Unknown error"
        }


# ---------------------------------------------------------
# Check Application Health
# ---------------------------------------------------------

def check_application_health():
    logger.info(
        "Checking application health"
    )

    command_id = send_ssm_command(
        [
            "curl -f --max-time 5 "
            "http://localhost:8000/health"
        ]
    )

    if not command_id:
        return False

    for attempt in range(6):
        time.sleep(2)

        result = get_ssm_result(
            command_id
        )

        status = result["status"]

        logger.info(
            "Health check attempt %s: %s",
            attempt + 1,
            status
        )

        if status == "Success":
            logger.info(
                "Application health check passed"
            )
            return True

        if status in {
            "Failed",
            "Cancelled",
            "TimedOut"
        }:
            break

    logger.warning(
        "Application health check failed"
    )

    return False


# ---------------------------------------------------------
# Restart Service
# ---------------------------------------------------------

def restart_service():
    logger.info(
        "Restarting service: %s",
        SERVICE_NAME
    )

    commands = [
        f"sudo systemctl restart {SERVICE_NAME}",
        f"sudo systemctl status {SERVICE_NAME} --no-pager"
    ]

    command_id = send_ssm_command(
        commands
    )

    if not command_id:
        return None

    return command_id


# ---------------------------------------------------------
# Store Incident in DynamoDB
# ---------------------------------------------------------

def store_incident(
    incident_id,
    alarm_name,
    alarm_reason,
    analysis,
    remediation_status,
    command_id=None,
    recovery_time=None
):
    try:
        item = {
            "incident_id": incident_id,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),

            "alarm_name": alarm_name,
            "alarm_reason": alarm_reason,

            "instance_id": EC2_INSTANCE_ID,
            "service_name": SERVICE_NAME,

            "remediation_status":
                remediation_status,

            "severity": analysis.get(
                "severity",
                "UNKNOWN"
            ),

            "root_cause": analysis.get(
                "root_cause",
                "UNKNOWN"
            ),

            "recommended_action":
                analysis.get(
                    "recommended_action",
                    "UNKNOWN"
                ),

            "confidence": Decimal(
                str(
                    analysis.get(
                        "confidence",
                        0
                    )
                )
            ),

            "reason": analysis.get(
                "reason",
                "UNKNOWN"
            )
        }

        if command_id:
            item["command_id"] = command_id

        if recovery_time:
            item["recovery_time"] = recovery_time

        table.put_item(
            Item=item
        )

        logger.info(
            "Incident stored in DynamoDB: %s",
            incident_id
        )

    except Exception:
        logger.exception(
            "Failed to store incident in DynamoDB"
        )


# ---------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------

def lambda_handler(
    event,
    context
):
    logger.info(
        "Received event: %s",
        json.dumps(event)
    )

    # -----------------------------------------------------
    # Read CloudWatch Alarm Event
    # -----------------------------------------------------

    alarm_name = event.get(
        "detail",
        {}
    ).get(
        "alarmName",
        "UnknownAlarm"
    )

    alarm_state = event.get(
        "detail",
        {}
    ).get(
        "state",
        {}
    ).get(
        "value",
        "UNKNOWN"
    )

    alarm_reason = event.get(
        "detail",
        {}
    ).get(
        "state",
        {}
    ).get(
        "reason",
        "Unknown reason"
    )

    logger.info(
        "Alarm: %s",
        alarm_name
    )

    logger.info(
        "Alarm state: %s",
        alarm_state
    )

    logger.info(
        "Alarm reason: %s",
        alarm_reason
    )

    # -----------------------------------------------------
    # Ignore Non-ALARM Events
    # -----------------------------------------------------

    if alarm_state != "ALARM":
        logger.info(
            "Alarm state is not ALARM. "
            "No remediation required."
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message":
                        "No action required",
                    "alarm_state":
                        alarm_state
                }
            )
        }

    # -----------------------------------------------------
    # Create Incident ID
    # -----------------------------------------------------

    incident_id = str(
        uuid.uuid4()
    )

    logger.info(
        "Incident ID: %s",
        incident_id
    )

    # -----------------------------------------------------
    # AI Analysis
    # -----------------------------------------------------

    analysis = analyze_incident_with_bedrock(
        alarm_name,
        alarm_reason
    )

    severity = analysis.get(
        "severity",
        "UNKNOWN"
    )

    root_cause = analysis.get(
        "root_cause",
        "UNKNOWN"
    )

    recommended_action = analysis.get(
        "recommended_action",
        "escalate"
    )

    confidence = float(
        analysis.get(
            "confidence",
            0
        )
    )

    logger.info(
        "AI Severity: %s",
        severity
    )

    logger.info(
        "AI Root Cause: %s",
        root_cause
    )

    logger.info(
        "AI Recommended Action: %s",
        recommended_action
    )

    logger.info(
        "AI Confidence: %.2f",
        confidence
    )

    # -----------------------------------------------------
    # SAFETY GATE
    # -----------------------------------------------------

    if (
        confidence < CONFIDENCE_THRESHOLD
        or recommended_action == "escalate"
        or recommended_action == "no_action"
    ):
        logger.warning(
            "Safety gate triggered. "
            "No automatic remediation."
        )

        store_incident(
            incident_id=incident_id,
            alarm_name=alarm_name,
            alarm_reason=alarm_reason,
            analysis=analysis,
            remediation_status="AI_ESCALATED"
        )

        send_sns_alert(
            incident_id=incident_id,
            alarm_name=alarm_name,
            alarm_reason=alarm_reason,
            analysis=analysis,
            remediation_status="AI_ESCALATED"
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "status":
                        "escalated",
                    "incident_id":
                        incident_id,
                    "reason":
                        "AI confidence below threshold "
                        "or escalation requested"
                }
            )
        }

    # -----------------------------------------------------
    # Restart Service
    # -----------------------------------------------------

    if recommended_action == "restart_service":

        for attempt in range(
            1,
            MAX_RETRIES + 1
        ):
            logger.info(
                "Remediation attempt %s/%s",
                attempt,
                MAX_RETRIES
            )

            command_id = restart_service()

            if not command_id:
                logger.warning(
                    "Restart command was not created"
                )
                continue

            logger.info(
                "Waiting for SSM command"
            )

            time.sleep(
                WAIT_SECONDS
            )

            result = get_ssm_result(
                command_id
            )

            logger.info(
                "SSM status: %s",
                result["status"]
            )

            logger.info(
                "SSM stdout: %s",
                result["stdout"]
            )

            logger.info(
                "SSM stderr: %s",
                result["stderr"]
            )

            if result["status"] != "Success":
                logger.warning(
                    "Service restart command failed"
                )
                continue

            # -------------------------------------------------
            # Wait Before Health Check
            # -------------------------------------------------

            time.sleep(3)

            healthy = check_application_health()

            if healthy:
                recovery_time = datetime.now(
                    timezone.utc
                ).isoformat()

                store_incident(
                    incident_id=incident_id,
                    alarm_name=alarm_name,
                    alarm_reason=alarm_reason,
                    analysis=analysis,
                    remediation_status="SUCCESS",
                    command_id=command_id,
                    recovery_time=recovery_time
                )

                logger.info(
                    "Service recovered successfully"
                )

                return {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "status":
                                "recovered",
                            "incident_id":
                                incident_id,
                            "attempt":
                                attempt
                        }
                    )
                }

        # -----------------------------------------------------
        # All Retries Failed
        # -----------------------------------------------------

        logger.error(
            "All remediation attempts failed"
        )

        store_incident(
            incident_id=incident_id,
            alarm_name=alarm_name,
            alarm_reason=alarm_reason,
            analysis=analysis,
            remediation_status="FAILED"
        )

        send_sns_alert(
            incident_id=incident_id,
            alarm_name=alarm_name,
            alarm_reason=alarm_reason,
            analysis=analysis,
            remediation_status="FAILED"
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "status":
                        "remediation_failed",
                    "incident_id":
                        incident_id
                }
            )
        }

    # -----------------------------------------------------
    # Restart Instance
    # -----------------------------------------------------

    if recommended_action == "restart_instance":

        logger.warning(
            "AI recommended EC2 instance restart. "
            "Manual approval required."
        )

        store_incident(
            incident_id=incident_id,
            alarm_name=alarm_name,
            alarm_reason=alarm_reason,
            analysis=analysis,
            remediation_status=
                "MANUAL_APPROVAL_REQUIRED"
        )

        send_sns_alert(
            incident_id=incident_id,
            alarm_name=alarm_name,
            alarm_reason=alarm_reason,
            analysis=analysis,
            remediation_status=
                "MANUAL_APPROVAL_REQUIRED"
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "status":
                        "manual_approval_required",
                    "incident_id":
                        incident_id
                }
            )
        }

    # -----------------------------------------------------
    # Fallback
    # -----------------------------------------------------

    logger.warning(
        "Unknown action. Escalating."
    )

    store_incident(
        incident_id=incident_id,
        alarm_name=alarm_name,
        alarm_reason=alarm_reason,
        analysis=analysis,
        remediation_status="ESCALATED"
    )

    send_sns_alert(
        incident_id=incident_id,
        alarm_name=alarm_name,
        alarm_reason=alarm_reason,
        analysis=analysis,
        remediation_status="ESCALATED"
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status":
                    "escalated",
                "incident_id":
                    incident_id
            }
        )
    }