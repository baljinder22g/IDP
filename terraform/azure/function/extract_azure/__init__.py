"""Azure Function — POST /v1/extract/azure
Document Intelligence (Form Recognizer) extracts layout/forms; Azure OpenAI
maps the extracted content into the caller's target JSON schema. Logs are
written to Blob Storage (the Logs tab can read them via /v1/logs).
"""
import base64
import datetime
import json
import logging
import os
import re
import time
import uuid

import azure.functions as func
import requests

DOCINT_ENDPOINT = os.environ.get("DOCINT_ENDPOINT", "").rstrip("/")
DOCINT_KEY = os.environ.get("DOCINT_KEY", "")
AOAI_ENDPOINT = os.environ.get("AOAI_ENDPOINT", "").rstrip("/")
AOAI_KEY = os.environ.get("AOAI_KEY", "")
AOAI_API_VERSION = os.environ.get("AOAI_API_VERSION", "2024-08-01-preview")
API_KEY = os.environ.get("API_KEY", "")

CORS = {
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,x-api-key",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json",
}

_PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\+?\d[\d\s-]{7,}\d"), "[PHONE]"),
    (re.compile(r"\b\d{6,}\b"), "[ID]"),
]


def mask_pii(v):
    if isinstance(v, str):
        for rx, r in _PII:
            v = rx.sub(r, v)
        return v
    if isinstance(v, dict):
        return {k: mask_pii(x) for k, x in v.items()}
    if isinstance(v, list):
        return [mask_pii(x) for x in v]
    return v


def _json(status, body):
    return func.HttpResponse(json.dumps(body, default=str), status_code=status, headers=CORS)


def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=200, headers=CORS)
    if API_KEY and req.headers.get("x-api-key") != API_KEY:
        return _json(401, {"error": "unauthorized", "message": "invalid x-api-key"})

    run_id = "run_" + uuid.uuid4().hex[:12]
    t0 = time.time()
    try:
        body = req.get_json()
        pdf = base64.b64decode(body["document_base64"])
        model = body.get("model", "prebuilt-layout")
        deployment = body.get("deployment", "gpt-4o-mapping")
        target_schema = body["target_schema"]

        content = _analyze_document(pdf, model)
        result = _map_with_openai(content, target_schema, deployment)
        latency = int((time.time() - t0) * 1000)

        out = {
            "run_id": run_id, "capability": "azure",
            "service": f"Azure Document Intelligence ({model}) + Azure OpenAI",
            "status": "succeeded", "latency_ms": latency, "result": result,
        }
        out["s3_key"] = _write_log(run_id, {"model": model, "filename": body.get("filename"),
                                            "result": result}, body.get("mask_pii", True))
        return _json(200, out)
    except Exception as e:
        logging.exception("azure extract failed")
        _write_log(run_id, {"error": str(e)}, True)
        return _json(500, {"error": "azure_failed", "message": str(e), "run_id": run_id})


def _analyze_document(pdf_bytes, model):
    """Call Document Intelligence (2023-07-31 GA REST API) and return its content text."""
    url = f"{DOCINT_ENDPOINT}/documentintelligence/documentModels/{model}:analyze?api-version=2024-02-29-preview"
    headers = {"Ocp-Apim-Subscription-Key": DOCINT_KEY, "Content-Type": "application/pdf"}
    r = requests.post(url, headers=headers, data=pdf_bytes)
    r.raise_for_status()
    op_loc = r.headers["Operation-Location"]
    for _ in range(30):
        time.sleep(1.5)
        poll = requests.get(op_loc, headers={"Ocp-Apim-Subscription-Key": DOCINT_KEY})
        poll.raise_for_status()
        data = poll.json()
        if data.get("status") == "succeeded":
            return data["analyzeResult"].get("content", "")
        if data.get("status") == "failed":
            raise RuntimeError("Document Intelligence analysis failed")
    raise TimeoutError("Document Intelligence timed out")


def _map_with_openai(content, target_schema, deployment):
    url = f"{AOAI_ENDPOINT}/openai/deployments/{deployment}/chat/completions?api-version={AOAI_API_VERSION}"
    headers = {"api-key": AOAI_KEY, "Content-Type": "application/json"}
    payload = {
        "messages": [
            {"role": "system", "content": "You extract insurance underwriting fields and return ONLY JSON matching the user's target schema. Use null/[] when unknown."},
            {"role": "user", "content": "Document content:\n" + content[:120000] +
                                        "\n\nTarget schema:\n" + target_schema},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
    }
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    return json.loads(text)


def _write_log(run_id, payload, mask):
    """Write a log blob using a connection string from app settings (best-effort)."""
    conn = os.environ.get("AzureWebJobsStorage", "")
    container = os.environ.get("LOG_CONTAINER", "idp-logs")
    if not conn:
        return None
    safe = mask_pii(payload) if mask else payload
    key = f"logs/azure/{datetime.date.today().isoformat()}/{run_id}.json"
    entry = {"timestamp": datetime.datetime.utcnow().isoformat() + "Z",
             "run_id": run_id, "capability": "azure", "detail": safe}
    try:
        from azure.storage.blob import BlobServiceClient
        svc = BlobServiceClient.from_connection_string(conn)
        try:
            svc.create_container(container)
        except Exception:
            pass
        svc.get_blob_client(container, key).upload_blob(
            json.dumps(entry, default=str), overwrite=True)
    except Exception as e:
        logging.warning("blob log failed: %s", e)
    return key
