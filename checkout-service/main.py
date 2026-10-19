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
