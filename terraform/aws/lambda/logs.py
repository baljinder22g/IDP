"""GET /v1/logs — list recent processing log objects from the S3 logs bucket."""
import json, os
import boto3
from common import respond, check_api_key, CORS, LOG_BUCKET

s3 = boto3.client("s3")


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS" or (event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS"):
        return {"statusCode": 200, "headers": CORS, "body": ""}
    unauth = check_api_key(event)
    if unauth:
        return unauth
    try:
        qs = event.get("queryStringParameters") or {}
        capability = qs.get("capability")
        limit = min(int(qs.get("limit", 50)), 200)
        prefix = f"logs/{capability}/" if capability else "logs/"

        keys = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=LOG_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append((obj["LastModified"], obj["Key"]))
        keys.sort(reverse=True)
        keys = keys[:limit]

        items = []
        for _, key in keys:
            try:
                body = s3.get_object(Bucket=LOG_BUCKET, Key=key)["Body"].read()
                entry = json.loads(body)
                d = entry.get("detail", {})
                items.append({
                    "timestamp": entry.get("timestamp"),
                    "run_id": entry.get("run_id"),
                    "capability": entry.get("capability"),
                    "model": d.get("model") or d.get("service") or "—",
                    "status": "error" if "error" in d else "succeeded",
                    "latency_ms": d.get("latency_ms"),
                    "s3_key": key,
                    "detail": entry,
                })
            except Exception as e:
                print("skip", key, e)
        return respond(200, {"items": items})
    except Exception as e:
        return respond(500, {"error": "logs_failed", "message": str(e)})
