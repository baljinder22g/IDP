"""Azure Function — GET /v1/logs  (lists log blobs from Blob Storage)."""
import json
import os

import azure.functions as func

CORS = {
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,x-api-key",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type": "application/json",
}
API_KEY = os.environ.get("API_KEY", "")


def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=200, headers=CORS)
    if API_KEY and req.headers.get("x-api-key") != API_KEY:
        return func.HttpResponse(json.dumps({"error": "unauthorized"}), status_code=401, headers=CORS)

    container = os.environ.get("LOG_CONTAINER", "idp-logs")
    conn = os.environ.get("AzureWebJobsStorage", "")
    limit = min(int(req.params.get("limit", 50)), 200)
    cap = req.params.get("capability")
    prefix = f"logs/{cap}/" if cap else "logs/"
    items = []
    try:
        from azure.storage.blob import BlobServiceClient
        svc = BlobServiceClient.from_connection_string(conn)
        client = svc.get_container_client(container)
        blobs = sorted(client.list_blobs(name_starts_with=prefix),
                       key=lambda b: b.last_modified, reverse=True)[:limit]
        for b in blobs:
            entry = json.loads(client.download_blob(b.name).readall())
            d = entry.get("detail", {})
            items.append({
                "timestamp": entry.get("timestamp"),
                "run_id": entry.get("run_id"),
                "capability": entry.get("capability"),
                "model": d.get("model") or d.get("service") or "—",
                "status": "error" if "error" in d else "succeeded",
                "latency_ms": d.get("latency_ms"),
                "s3_key": b.name,
                "detail": entry,
            })
    except Exception as e:
        return func.HttpResponse(json.dumps({"items": items, "warning": str(e)}),
                                 status_code=200, headers=CORS)
    return func.HttpResponse(json.dumps({"items": items}, default=str), status_code=200, headers=CORS)
