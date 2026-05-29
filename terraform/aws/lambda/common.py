"""Shared helpers for IDP Lambda functions: CORS, PII masking, S3 logging."""
import json, os, re, time, uuid, datetime

LOG_BUCKET = os.environ.get("LOG_BUCKET", "")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "")

CORS = {
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,x-api-key",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}

_PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\+?\d[\d\s-]{7,}\d"), "[PHONE]"),
    (re.compile(r"\b\d{6,}\b"), "[ID]"),
]


def mask_pii(value):
    """Recursively mask common PII patterns in a value (str/dict/list)."""
    if isinstance(value, str):
        out = value
        for rx, repl in _PII:
            out = rx.sub(repl, out)
        return out
    if isinstance(value, dict):
        return {k: mask_pii(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_pii(v) for v in value]
    return value


def respond(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def check_api_key(event):
    """If API_KEY is set in the environment, require a matching x-api-key header.
    Returns an error response dict when unauthorized, else None."""
    expected = os.environ.get("API_KEY", "")
    if not expected:
        return None
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-api-key") != expected:
        return {"statusCode": 401, "headers": CORS,
                "body": json.dumps({"error": "unauthorized", "message": "invalid or missing x-api-key"})}
    return None


def new_run_id():
    return "run_" + uuid.uuid4().hex[:12]


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def write_log(s3_client, capability, run_id, payload, mask=True):
    """Write a structured log object to the S3 logs bucket. Returns the key."""
    if not LOG_BUCKET:
        return None
    safe = mask_pii(payload) if mask else payload
    key = f"logs/{capability}/{datetime.date.today().isoformat()}/{run_id}.json"
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "run_id": run_id,
        "capability": capability,
        "detail": safe,
    }
    try:
        s3_client.put_object(
            Bucket=LOG_BUCKET, Key=key,
            Body=json.dumps(entry, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:  # never fail the request because logging failed
        print("log write failed:", e)
    return key
