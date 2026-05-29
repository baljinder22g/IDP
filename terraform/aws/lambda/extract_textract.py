"""POST /v1/extract/textract — Textract OCR/forms, then Bedrock maps to target schema.

Multi-page PDFs require the asynchronous Textract API, which reads from S3. So we:
  1. upload the PDF to the input bucket
  2. start_document_analysis  ->  poll get_document_analysis
  3. collect KEY_VALUE_SET + TABLE blocks into plain key/values
  4. ask Bedrock to reshape those key/values into the target schema
"""
import base64, json, os, time
import boto3
from common import respond, parse_body, new_run_id, write_log, check_api_key, CORS, INPUT_BUCKET

REGION = os.environ.get("AWS_REGION", "eu-west-1")
MODEL = os.environ.get("BEDROCK_MODEL", "anthropic.claude-sonnet-4-6-v1:0")
MAX_POLLS = int(os.environ.get("TEXTRACT_MAX_POLLS", "20"))

textract = boto3.client("textract", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3 = boto3.client("s3")


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
        pdf = base64.b64decode(body["document_base64"])
        features = body.get("feature_types", ["FORMS", "TABLES"])
        key = f"input/{run_id}.pdf"
        s3.put_object(Bucket=INPUT_BUCKET, Key=key, Body=pdf, ContentType="application/pdf")

        job = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": INPUT_BUCKET, "Name": key}},
            FeatureTypes=[f for f in features if f in ("FORMS", "TABLES", "SIGNATURES")] or ["FORMS"],
        )
        blocks = _poll(job["JobId"])
        kv = _key_values(blocks)

        result = _map_with_bedrock(kv, body["target_schema"])
        latency = int((time.time() - t0) * 1000)

        out = {
            "run_id": run_id, "capability": "textract",
            "service": "AWS Textract + Bedrock mapping", "status": "succeeded",
            "latency_ms": latency, "raw_keyvalues": kv, "result": result,
        }
        out["s3_key"] = write_log(
            s3, "textract", run_id,
            {"filename": body.get("filename"), "raw_keyvalues": kv, "result": result},
            mask=body.get("mask_pii", True),
        )
        return respond(200, out)
    except Exception as e:
        write_log(s3, "textract", run_id, {"error": str(e)}, mask=True)
        return respond(500, {"error": "textract_failed", "message": str(e), "run_id": run_id})


def _poll(job_id):
    blocks = []
    for _ in range(MAX_POLLS):
        r = textract.get_document_analysis(JobId=job_id)
        status = r["JobStatus"]
        if status == "SUCCEEDED":
            blocks.extend(r.get("Blocks", []))
            token = r.get("NextToken")
            while token:
                r2 = textract.get_document_analysis(JobId=job_id, NextToken=token)
                blocks.extend(r2.get("Blocks", []))
                token = r2.get("NextToken")
            return blocks
        if status == "FAILED":
            raise RuntimeError("Textract job failed: " + r.get("StatusMessage", ""))
        time.sleep(1.5)
    raise TimeoutError("Textract job did not finish in time")


def _key_values(blocks):
    """Reconstruct KEY_VALUE_SET pairs from Textract blocks."""
    by_id = {b["Id"]: b for b in blocks}

    def words(block):
        out = []
        for rel in block.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    c = by_id.get(cid, {})
                    if c.get("BlockType") == "WORD":
                        out.append(c.get("Text", ""))
                    elif c.get("BlockType") == "SELECTION_ELEMENT" and c.get("SelectionStatus") == "SELECTED":
                        out.append("[X]")
        return " ".join(out)

    kv = {}
    for b in blocks:
        if b.get("BlockType") == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
            key_text = words(b)
            value_text = ""
            for rel in b.get("Relationships", []):
                if rel["Type"] == "VALUE":
                    for vid in rel["Ids"]:
                        value_text = words(by_id.get(vid, {}))
            if key_text:
                kv[key_text.strip(": ")] = value_text.strip()
    return kv


def _map_with_bedrock(kv, target_schema):
    prompt = (
        "Map the following key/value pairs extracted from an insurance broker "
        "document into the target JSON schema. Return only JSON matching the schema; "
        "use null/[] when unknown.\n\nKey/Values:\n" + json.dumps(kv, indent=2) +
        "\n\nTarget schema:\n" + target_schema
    )
    resp = bedrock.converse(
        modelId=MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 4096, "temperature": 0},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip("` \n")
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
