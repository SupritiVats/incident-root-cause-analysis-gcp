# SRE Incident Root-Cause Agent — Complete Build Guide

This guide contains everything you need to rebuild the project from scratch: the architecture, the source code, and every step. Replace any placeholder like `<YOUR_PROJECT_ID>` or `<YOUR_REGION>` with your own values. No project-specific secrets or account details are included.

---

## What this project does

This is an event-driven SRE agent on Google Cloud. When a real production service starts throwing errors:

1. Cloud Monitoring notices the error spike.
2. An Alerting Policy publishes a message to a Pub/Sub topic.
3. A Cloud Function receives the message.
4. Gemini decides whether to check **logs** or **metrics**.
5. The function fetches the real evidence using a read-only service account.
6. Gemini writes a root-cause analysis with confidence and a fix suggestion.
7. The result is posted to a Slack channel.

No human opens a dashboard. No API keys are stored in the code.

---

## Architecture

```
checkout-service (Cloud Run)
        │ real logs + built-in metrics
        ▼
Cloud Logging + Cloud Monitoring
        │ error spike crosses threshold
        ▼
Alerting Policy ──► Pub/Sub topic `incident-alerts`
        │
        ▼
incident-agent (Cloud Function Gen2)
        │ Gemini 2.5 decides: logs or metrics
        │ fetches real evidence, read-only
        │ Gemini 2.5 synthesizes RCA
        ▼
Slack #incident-agent-alerts
```

---

## Project files

| File | Purpose |
|---|---|
| `checkout-service/main.py` | Flask target service with `/healthz`, `/checkout`, `/simulate-incident`, `/resolve-incident` |
| `checkout-service/requirements.txt` | Flask + Gunicorn dependencies |
| `checkout-service/Procfile` | Gunicorn startup command |
| `incident-agent/main.py` | Cloud Function that receives Pub/Sub, runs the Gemini investigation, and posts to Slack |
| `incident-agent/requirements.txt` | Functions Framework, GCP clients, Vertex AI |

---

## Safety design

The `incident-agent` function runs as a dedicated service account `sre-agent-readonly` with exactly three roles:

- `roles/logging.viewer`
- `roles/monitoring.viewer`
- `roles/aiplatform.user`

It has **no write or delete permission** anywhere. Even if the model misbehaves, it cannot change production.

---

## Wow factors

1. **AI decides the data source.** Nothing hardcodes "check logs" or "check metrics". Gemini reads the alert text and picks.
2. **Real production evidence.** The agent investigates actual Cloud Logging and Cloud Monitoring data, not fixtures.
3. **Event-driven end-to-end.** A real error condition opens a Cloud Monitoring incident, which triggers the investigation automatically.
4. **Read-only by infrastructure.** The service account is the real safety, not a keyword filter.
5. **No API keys.** Vertex AI and GCP clients use the function's own identity.

---

## Prerequisites

1. A Google Cloud project with billing enabled.
2. Owner or Editor IAM role on that project.
3. A Slack workspace where you can create an app and a channel.
4. Cloud Shell or `gcloud` CLI installed.

---

## Step 1 — Enable the APIs

### Via the Google Cloud Console (UI)

1. Open `console.cloud.google.com` and select your project from the top project dropdown.
2. Click the navigation menu (hamburger icon) → **APIs & Services → Library**.
3. For each API listed below, do this exactly:
   - Type the API name in the search box and press Enter.
   - Click the matching result.
   - Click the **Enable** button.
   - Wait for the blue checkmark or "API enabled" message, then go back to the Library search.

Enable all of these:

- Cloud Run API
- Cloud Functions API
- Cloud Build API
- Cloud Logging API
- Cloud Monitoring API
- Cloud Pub/Sub API
- Vertex AI API
- Eventarc API

### Via Cloud Shell (gcloud)

Open the Cloud Shell terminal in the Console (the `>_` icon at the top right) and run:

```bash
gcloud services enable run.googleapis.com \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  pubsub.googleapis.com \
  aiplatform.googleapis.com \
  eventarc.googleapis.com \
  --project=<YOUR_PROJECT_ID>
```

---

## Step 2 — Create the read-only service account

1. Go to **IAM & Admin → Service Accounts**.
2. Click **Create Service Account**.
3. Name: `sre-agent-readonly`.
4. Grant these roles:
   - **Logs Viewer**
   - **Monitoring Viewer**
   - **Vertex AI User**
5. Click **Done**.

This account must have **no other roles**.

---

## Step 3 — Deploy `checkout-service`

This is the target application the agent will investigate.

Open the Cloud Shell terminal in the Console (the `>_` icon at the top right) and run:

```bash
mkdir -p ~/checkout-service && cd ~/checkout-service
```

Create the three files below.

`main.py`:

```python
import json, os, random, sys, time
from flask import Flask, jsonify

app = Flask(__name__)
SERVICE_NAME = "checkout-service"
STATE = {"incident": False, "pool_size": 25}

def log(severity, message, **fields):
    print(json.dumps({"severity": severity, "message": message, "service": SERVICE_NAME, **fields}))
    sys.stdout.flush()

@app.get("/healthz")
def healthz():
    return jsonify(status="ok")

@app.get("/checkout")
def checkout():
    trace_id = f"req-{random.randint(1000,9999)}"
    if STATE["incident"]:
        time.sleep(random.uniform(3, 5))
        if random.random() < 0.85:
            log("ERROR", "Timeout acquiring connection from pool after 5000ms",
                trace_id=trace_id, pool_size=STATE["pool_size"])
            return jsonify(error="order creation failed", trace_id=trace_id), 500
    else:
        time.sleep(random.uniform(0.05, 0.2))
    log("INFO", "Order created successfully", trace_id=trace_id)
    return jsonify(status="ok", trace_id=trace_id)

@app.get("/simulate-incident")
def simulate_incident():
    STATE["incident"] = True
    STATE["pool_size"] = 10
    log("WARNING", "Pool size reduced after deploy v2.14.0 (max=10, previous max=25)")
    return jsonify(message="Incident mode ON. Hit /checkout repeatedly for ~1-2 min.")

@app.get("/resolve-incident")
def resolve_incident():
    STATE["incident"] = False
    STATE["pool_size"] = 25
    log("INFO", "Pool size restored to 25, incident resolved")
    return jsonify(message="Incident mode OFF.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

`requirements.txt`:

```text
flask==3.0.3
gunicorn==22.0.0
```

`Procfile`:

```text
web: gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 30 main:app
```

Then deploy:

```bash
gcloud run deploy checkout-service \
  --source=. \
  --region=<YOUR_REGION> \
  --allow-unauthenticated \
  --project=<YOUR_PROJECT_ID>
```

This `gcloud` command already runs inside the Console — open **Cloud Shell** (the `>_` icon at the top right) and paste it. That is the fastest path. There is no separate point-and-click source upload for a full Flask app in the Cloud Run Console, so this is the recommended way.

When it finishes, copy the service URL and test it:

```bash
curl https://<YOUR_CHECKOUT_SERVICE_URL>/healthz
curl https://<YOUR_CHECKOUT_SERVICE_URL>/checkout
```

---

## Step 4 — Create the Pub/Sub topic

1. Go to **Pub/Sub → Topics**.
2. Click **Create Topic**.
3. Topic ID: `incident-alerts`.
4. Click **Create**.

This is the message bus between Cloud Monitoring and the agent.

---

## Step 5 — Create the log-based metric

1. Go to **Logging → Logs Explorer**.
2. Paste this query:

   ```
   resource.type="cloud_run_revision"
   resource.labels.service_name="checkout-service"
   severity=ERROR
   ```

3. Click **Actions → Create metric**.
4. Metric name: `checkout_error_count`.
5. Type: **Counter**.
6. Click **Create Metric**.

This metric counts every `ERROR` log from `checkout-service`.

---

## Step 6 — Create the Slack app and Incoming Webhook

1. Open `https://api.slack.com/apps`.
2. Click **Create New App → From scratch**.
3. App name: `Incident Agent`.
4. Pick your workspace.
5. Click **Create App**.
6. In the left sidebar, click **Incoming Webhooks**.
7. Toggle **On**.
8. Click **Add New Webhook to Workspace**.
9. Select a channel, for example `#incident-agent-alerts`.
10. Click **Allow**.
11. Copy the webhook URL. You will paste it in Step 9.

**Keep the webhook URL private.** Do not commit it.

---

## Step 7 — Create the Pub/Sub notification channel

1. Go to **Monitoring → Alerting → Edit Notification Channels**.
2. Find the **Pub/Sub** section.
3. Click **Add New**.
4. Select the `incident-alerts` topic.
5. Click **Save**.

---

## Step 8 — Create the Alerting Policy

1. Go to **Monitoring → Alerting → Create Policy**.
2. Click **Select a metric**.
3. Search for `checkout_error_count` under **Logs-based Metrics**.
4. In the condition:
   - Rolling window: **1 minute**
   - Threshold: **above 3**
5. Click **Next**.
6. Add the Pub/Sub notification channel from Step 7.
7. Name the policy: `Checkout Service Error Spike`.
8. Click **Create Policy**.

---

## Step 9 — Deploy `incident-agent`

This is the Cloud Function that receives the Pub/Sub alert, uses Gemini to decide what evidence to fetch, fetches real logs or metrics, and posts the root cause to Slack.

Open the Cloud Shell terminal in the Console (the `>_` icon at the top right) and run:

```bash
mkdir -p ~/incident-agent && cd ~/incident-agent
```

Create the two files below.

`main.py`:

```python
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
# just replying with plain text.
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
```

`requirements.txt`:

```text
functions-framework==3.8.1
requests==2.32.3
google-cloud-logging==3.11.3
google-cloud-monitoring==2.22.2
google-cloud-aiplatform==1.70.0
```

Then deploy:

```bash
gcloud functions deploy incident-agent \
  --gen2 \
  --runtime=python312 \
  --region=<YOUR_REGION> \
  --source=. \
  --entry-point=investigate \
  --trigger-topic=incident-alerts \
  --service-account=sre-agent-readonly@<YOUR_PROJECT_ID>.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --set-env-vars=GCP_PROJECT=<YOUR_PROJECT_ID>,TARGET_SERVICE_NAME=checkout-service,TARGET_ENV=prod,VERTEX_LOCATION=us-central1,SLACK_WEBHOOK_URL=<YOUR_WEBHOOK_URL> \
  --project=<YOUR_PROJECT_ID>
```

### Alternative: deploy as a Cloud Run function from the Console (no Docker)

1. Go to **Cloud Run**.
2. Click **Create Service**.
3. At the top, click the **Function** tab (next to Artifact Registry, Docker Hub, etc.).
4. Service name: `incident-agent`.
5. Region: `<YOUR_REGION>`.
6. Runtime: **Python 3.12**.
7. Entry point: `investigate`.
8. Source code: use the inline editor and paste the full `main.py` contents, then also provide the `requirements.txt` content in the dependencies/inline editor panel.
9. Trigger: click **Add trigger** → choose **Cloud Pub/Sub** → select the `incident-alerts` topic.
10. Service account: `sre-agent-readonly@<YOUR_PROJECT_ID>.iam.gserviceaccount.com`.
11. Environment variables — add each one:
    - `GCP_PROJECT` = `<YOUR_PROJECT_ID>`
    - `TARGET_SERVICE_NAME` = `checkout-service`
    - `TARGET_ENV` = `prod`
    - `VERTEX_LOCATION` = `us-central1`
    - `SLACK_WEBHOOK_URL` = `<YOUR_WEBHOOK_URL>`
12. Click **Create**.

Wait for the deployment to finish.

---

## Step 10 — Fix the IAM invocation permissions

After the first deploy, Pub/Sub may not be able to trigger the function. Fix this:

### 10a — Allow the agent's service account to invoke the function

1. Go to **Cloud Run**.
2. Find the function service, which is named `incident-agent-...`.
3. Click the service.
4. Go to the **Permissions** tab.
5. Click **Add Principal**.
6. Principal: `sre-agent-readonly@<YOUR_PROJECT_ID>.iam.gserviceaccount.com`.
7. Role: **Cloud Run Invoker**.
8. Click **Save**.

### 10b — Allow Pub/Sub to create tokens

1. Go to **IAM & Admin → IAM**.
2. Click **Grant Access**.
3. Principal: `service-<YOUR_PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com`.
4. Role: **Service Account Token Creator**.
5. Click **Save**.

Replace `<YOUR_PROJECT_NUMBER>` with your actual project number.

---

## Step 11 — Test the full pipeline

### 11a — Reset the service

```bash
curl https://<YOUR_CHECKOUT_SERVICE_URL>/resolve-incident
```

### 11b — Turn on incident mode

```bash
curl https://<YOUR_CHECKOUT_SERVICE_URL>/simulate-incident
```

### 11c — Generate traffic

```bash
for i in $(seq 1 40); do
  curl -s https://<YOUR_CHECKOUT_SERVICE_URL>/checkout > /dev/null &
done
wait
```

### 11d — Watch the chain fire

1. **Cloud Run → `checkout-service` → Logs**: real `ERROR` lines appear.
2. **Monitoring → Alerting → `Checkout Service Error Spike`**: the chart crosses the threshold and an incident opens.
3. **Cloud Run → `incident-agent` → Logs**: you will see `--- NEW ALERT TRIGGERED ---`, then `Diagnose decided resource=...`, then the synthesized root cause.
4. **Slack**: the `#incident-agent-alerts` channel receives the RCA message.

### 11e — Resolve

```bash
curl https://<YOUR_CHECKOUT_SERVICE_URL>/resolve-incident
```

---

## Optional — Manual Pub/Sub test

To test the agent without waiting for the Alerting Policy, you can publish a test message directly to the Pub/Sub topic.

### Via the Console (UI)

1. Go to **Pub/Sub → Topics**.
2. Click the `incident-alerts` topic.
3. Click **Publish Message**.
4. In the **Message body** field, paste exactly:
   ```json
   {"incident":{"incident_id":"manual-test-1","state":"open","summary":"Manual test alert","condition_name":"test"}}
   ```
5. Click **Publish**.

### Via Cloud Shell (gcloud)

Open the Cloud Shell terminal and run:

```bash
gcloud pubsub topics publish incident-alerts \
  --message='{"incident":{"incident_id":"manual-test-1","state":"open","summary":"Manual test alert","condition_name":"test"}}' \
  --project=<YOUR_PROJECT_ID>
```

Check the `incident-agent` logs and Slack for the result.

---

## Expected end state

- `checkout-service` is live on Cloud Run and can be broken/reset.
- `incident-agent` is deployed as a Gen2 Cloud Function with a Pub/Sub trigger.
- The `sre-agent-readonly` service account has only `Logs Viewer`, `Monitoring Viewer`, and `Vertex AI User`.
- An Alerting Policy watches `checkout_error_count` and publishes to `incident-alerts`.
- A real error spike in `checkout-service` auto-triggers an RCA message in Slack.
