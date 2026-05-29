"""POST /v1/extract/bedrock — send the PDF straight to a Bedrock model."""
import base64, json, os, time
import boto3
from common import respond, parse_body, new_run_id, write_log, check_api_key, CORS

REGION = os.environ.get("AWS_REGION", "eu-west-1")
DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL", "anthropic.claude-sonnet-4-6-v1:0")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3 = boto3.client("s3")

SYSTEM = (
    "You are an insurance underwriting document extraction engine. "
    "Read the attached broker document (which may contain a medical questionnaire, "
    "KYC/IDP identity pages, financials and free text for a high-net-worth client). "
    "Extract the requested fields and return ONLY a JSON object that matches the "
    "provided target schema exactly — same keys and nesting. Use null or [] when a "
    "value is not present. Do not invent values. Include an 'extraction_confidence' "
    "between 0 and 1 if present in the schema."
)


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS" or (event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS"):
        return {"statusCode": 200, "headers": CORS, "body": ""}
    unauth = check_api_key(event)
    if unauth:
        return unauth
    run_id = new_run_id()
    t0 = time.time()
    try:
        body = parse_body(event)
        pdf_bytes = base64.b64decode(body["document_base64"])
        target_schema = body["target_schema"]
        model_id = body.get("model", DEFAULT_MODEL)

        resp = bedrock.converse(
            modelId=model_id,
            system=[{"text": SYSTEM}],
            messages=[{
                "role": "user",
                "content": [
                    {"document": {
                        "format": "pdf",
                        "name": "broker_document",
                        "source": {"bytes": pdf_bytes},
                    }},
                    {"text": "Target JSON schema:\n" + target_schema +
                             "\n\nReturn only the populated JSON object."},
                ],
            }],
            inferenceConfig={"maxTokens": 4096, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        result = _coerce_json(text)
        latency = int((time.time() - t0) * 1000)

        out = {
            "run_id": run_id, "capability": "bedrock", "model": model_id,
            "status": "succeeded", "latency_ms": latency, "result": result,
        }
        out["s3_key"] = write_log(
            s3, "bedrock", run_id,
            {"model": model_id, "filename": body.get("filename"), "result": result},
            mask=body.get("mask_pii", True),
        )
        return respond(200, out)
    except Exception as e:
        write_log(s3, "bedrock", run_id, {"error": str(e)}, mask=True)
        return respond(500, {"error": "bedrock_failed", "message": str(e), "run_id": run_id})


def _coerce_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip("` \n")
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise
