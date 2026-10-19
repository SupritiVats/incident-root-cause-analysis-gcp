import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import functions_framework
import requests
import vertexai
from google.cloud import logging as gcp_logging
from google.cloud import monitoring_v3
from vertexai.generative_models import FunctionDeclaration, GenerativeModel, Tool, ToolConfig

PROJECT_ID = os.environ.get("GCP_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT"))
SERVICE_NAME = os.environ.get("TARGET_SERVICE_NAME", "checkout-service")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")

# --- Safety: the only hardcoded guard. Real safety is the read-only IAM
# role on this function's service account (logging.viewer + monitoring.viewer) ---
BLOCKED_PATTERN = re.compile(r"delete|drop|update|insert|truncate|alter|create|grant|revoke|exec", re.IGNORECASE)
_seen_incidents = set()


def validate_query(resource, query):
    if resource not in ("logs", "metrics"):
        raise ValueError(f"Unsafe resource requested: {resource!r}")
    if query and BLOCKED_PATTERN.search(query):
        raise ValueError(f"Blocked unsafe query: {query!r}")


# --- Gemini via Vertex AI (uses this function's own service account, no API key) ---
vertexai.init(project=PROJECT_ID, location=VERTEX_LOCATION)

FETCH_TOOL = Tool(function_declarations=[
    FunctionDeclaration(
        name="fetch_observability",
        description="Fetch logs or metrics needed to investigate the alert",
        parameters={
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": ["logs", "metrics"]},
                "query": {"type": "string", "description": "short filter/keyword to look for"},
                "time_range": {"type": "string", "description": "e.g. '15m', '1h'"},
            },
            "required": ["resource", "query", "time_range"],
        },
    )
])

# Forces Gemini to actually call fetch_observability instead of sometimes
# just replying with plain text (which was the root cause of the earlier
# "'NoneType' object has no attribute 'args'" failure).
FETCH_TOOL_CONFIG = ToolConfig(
    function_calling_config=ToolConfig.FunctionCallingConfig(
        mode=ToolConfig.FunctionCallingConfig.Mode.ANY,
    )
)


def diagnose(alert, env):
    model = GenerativeModel(
        "gemini-2.5-flash",
        tools=[FETCH_TOOL],
        tool_config=FETCH_TOOL_CONFIG,
        system_instruction=(
            "You are an SRE observability router. Available resources: 'logs' "
            "(fields: timestamp, severity, service, message, trace_id) and 'metrics' "
            "(error_rate, latency_p99, cpu, memory, pod_restarts). Never invent a "
            "mutation query. Respond ONLY by calling fetch_observability."
        ),
    )
    resp = model.generate_content(f"Alert: {alert}. Environment: {env}. Decide what to fetch.")
    part = resp.candidates[0].content.parts[0]

    if not part.function_call or not part.function_call.name:
        raise ValueError(f"Gemini did not call fetch_observability; got text instead: {part.text!r}")

    args = part.function_call.args
    return {"resource": args["resource"], "query": args["query"], "time_range": args["time_range"]}


def synthesize(alert, env, resource, query, results):
    model = GenerativeModel("gemini-2.5-flash")
    prompt = (
        f"Alert: {alert}\nEnvironment: {env}\nResource checked: {resource}\nQuery used: {query}\n"
        f"Results: {json.dumps(results)}\n\n"
        "Write a short Slack message: one-line probable root cause with confidence "
        "(low/med/high), the specific evidence, a fix suggestion scoped to the "
        "environment, and say plainly if the data is empty/inconclusive instead of guessing."
    )
    resp = model.generate_content(prompt)
    return resp.text


# --- Real, read-only GCP data fetchers (with retry for ingestion delay) ---
_logging_client = gcp_logging.Client(project=PROJECT_ID)
_monitoring_client = monitoring_v3.MetricServiceClient()


def _minutes(time_range, default=15):
    try:
        if time_range.endswith("h"):
            return int(float(time_range[:-1]) * 60)
        if time_range.endswith("m"):
            return int(time_range[:-1])
    except Exception:
        pass
    return default


def fetch_logs(query, time_range):
    since = (datetime.now(timezone.utc) - timedelta(minutes=_minutes(time_range))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    filter_str = (
        f'resource.type="cloud_run_revision" resource.labels.service_name="{SERVICE_NAME}" '
        f'timestamp>="{since}"'
    )
    if query:
        filter_str += f" AND {query}"

    entries = []
    for attempt in range(3):
        entries = []
        for entry in _logging_client.list_entries(filter_=filter_str, order_by=gcp_logging.DESCENDING, page_size=25):
            payload = entry.payload if isinstance(entry.payload, dict) else {"message": entry.payload}
            entries.append({"timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                             "severity": entry.severity, **payload})
        if entries:
            break
        time.sleep(3)  # log ingestion delay
    return {"entries": entries}


def _time_series(metric_type, minutes):
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval({
        "end_time": {"seconds": int(now.timestamp())},
        "start_time": {"seconds": int((now - timedelta(minutes=minutes)).timestamp())},
    })
    results = _monitoring_client.list_time_series(request={
        "name": f"projects/{PROJECT_ID}",
        "filter": f'metric.type="{metric_type}" AND resource.labels.service_name="{SERVICE_NAME}"',
        "interval": interval,
        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    })
    series = []
    for ts in results:
        points = [{"time": p.interval.end_time.isoformat(),
                    "value": p.value.double_value or p.value.int64_value} for p in ts.points]
        series.append({"labels": dict(ts.resource.labels), "points": points})
    return series


def fetch_metrics(query, time_range):
    minutes = _minutes(time_range)
    return {
        "request_count": _time_series("run.googleapis.com/request_count", minutes),
        "request_latencies": _time_series("run.googleapis.com/request_latencies", minutes),
    }


def post_to_slack(message):
    if not SLACK_WEBHOOK_URL:
        print("No SLACK_WEBHOOK_URL configured; message:", message)
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"response_type": "in_channel", "text": message}, timeout=10)
    except Exception as e:
        print("Failed to post to Slack:", e)


@functions_framework.cloud_event
def investigate(cloud_event):
    print("--- NEW ALERT TRIGGERED ---")
    try:
        data = cloud_event.data["message"]["data"]
        payload = json.loads(base64.b64decode(data).decode("utf-8"))
    except Exception as e:
        print("Could not decode Pub/Sub message:", e)
        return

    incident = payload.get("incident", {})
    if incident.get("state") != "open":
        print("Ignoring non-open incident notification")
        return

    incident_id = incident.get("incident_id", "")
    if incident_id in _seen_incidents:
        print("Already investigating this incident, skipping")
        return
    _seen_incidents.add(incident_id)

    alert = incident.get("summary") or incident.get("condition_name") or "Unnamed incident"
    env = os.environ.get("TARGET_ENV", "prod")

    try:
        decision = diagnose(alert, env)
        resource, query, time_range = decision["resource"], decision["query"], decision["time_range"]
        print(f"Diagnose decided resource={resource} query={query!r} time_range={time_range}")

        validate_query(resource, query)

        results = fetch_logs(query, time_range) if resource == "logs" else fetch_metrics(query, time_range)
        message = synthesize(alert, env, resource, query, results)
        print("Synthesized:", message)
    except Exception as e:
        message = f":warning: Investigation failed: {e}"
        print(message)

    post_to_slack(message)
