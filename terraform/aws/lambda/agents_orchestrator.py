"""POST /v1/agents/run — multi-agent pipeline.

Agents run sequentially inside one Lambda (simple, free-tier friendly):
  1. Ingest      — store PDF in S3
  2. OCR         — Textract analyze
  3. PII Mask    — mask detected PII in the key/values
  4. Extract     — Bedrock maps masked key/values to the target schema
  5. Validate    — check required keys are present

Each agent returns {input, output} so the UI can render per-agent boxes.
For larger production workloads this maps cleanly onto AWS Step Functions
(one state per agent) — see docs/documentation.md.
"""
import base64, json, os, time
import boto3
from common import respond, parse_body, new_run_id, write_log, mask_pii, check_api_key, CORS, INPUT_BUCKET

REGION = os.environ.get("AWS_REGION", "eu-west-1")
MODEL = os.environ.get("BEDROCK_MODEL", "anthropic.claude-sonnet-4-6-v1:0")

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
    steps = []
    try:
        body = parse_body(event)
        pdf = base64.b64decode(body["document_base64"])
        schema = body["target_schema"]
        fname = body.get("filename", "document.pdf")

        # 1 — Ingest
        key = f"input/{run_id}.pdf"
        s3.put_object(Bucket=INPUT_BUCKET, Key=key, Body=pdf, ContentType="application/pdf")
        steps.append(_step("ingest", "Ingest Agent", "📥", "S3 / pre-flight",
                           {"file": fname, "size_kb": round(len(pdf) / 1024)},
                           {"stored": f"s3://{INPUT_BUCKET}/{key}", "mime": "application/pdf"}))

        # 2 — OCR
        job = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": INPUT_BUCKET, "Name": key}},
            FeatureTypes=["FORMS", "TABLES"])
        blocks = _poll(job["JobId"])
        kv = _key_values(blocks)
        steps.append(_step("ocr", "OCR Agent", "🔎", "AWS Textract",
                           {"s3_key": key}, {"kv_pairs": len(kv), "blocks": len(blocks)}))

        # 3 — PII mask
        masked = mask_pii(kv)
        pii_fields = [k for k, v in kv.items() if v != masked.get(k)]
        steps.append(_step("pii", "PII Masking Agent", "🛡️", "regex / Comprehend",
                           {"kv_pairs": len(kv)}, {"pii_found": len(pii_fields), "masked_fields": pii_fields}))

        # 4 — Extract
        result = _extract(masked, schema)
        steps.append(_step("extract", "Extraction Agent", "🧠", "AWS Bedrock", {"masked": True}, result))

        # 5 — Validate
        missing = _validate(result, schema)
        steps.append(_step("validate", "Validation Agent", "✅", "Schema validator",
                           {"against": "target_schema"}, {"valid": not missing, "missing_required": missing}))

        latency = int((time.time() - t0) * 1000)
        out = {"run_id": run_id, "capability": "agents", "status": "succeeded",
               "latency_ms": latency, "steps": steps, "result": result}
        out["s3_key"] = write_log(s3, "agents", run_id,
                                  {"filename": fname, "steps": steps, "result": result},
                                  mask=body.get("mask_pii", True))
        return respond(200, out)
    except Exception as e:
        write_log(s3, "agents", run_id, {"error": str(e), "steps": steps}, mask=True)
        return respond(500, {"error": "agents_failed", "message": str(e), "run_id": run_id, "steps": steps})


def _step(sid, title, icon, svc, inp, outp):
    return {"id": sid, "title": title, "icon": icon, "svc": svc, "input": inp, "output": outp}


def _extract(kv, schema):
    prompt = ("Map these masked key/values into the target JSON schema. Return only JSON.\n\n"
              + json.dumps(kv, indent=2) + "\n\nTarget schema:\n" + schema)
    resp = bedrock.converse(modelId=MODEL,
                            messages=[{"role": "user", "content": [{"text": prompt}]}],
                            inferenceConfig={"maxTokens": 4096, "temperature": 0})
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip("` \n")
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])


def _validate(result, schema_str):
    try:
        schema = json.loads(schema_str)
    except Exception:
        return []
    missing = []

    def walk(s, r, path=""):
        if isinstance(s, dict):
            for k in s:
                p = f"{path}.{k}" if path else k
                if not isinstance(r, dict) or k not in r or r.get(k) in (None, ""):
                    missing.append(p)
                else:
                    walk(s[k], r[k], p)
    walk(schema, result)
    return missing[:25]


# --- Textract helpers (same logic as extract_textract) ---
def _poll(job_id):
    blocks = []
    for _ in range(20):
        r = textract.get_document_analysis(JobId=job_id)
        if r["JobStatus"] == "SUCCEEDED":
            blocks.extend(r.get("Blocks", []))
            tok = r.get("NextToken")
            while tok:
                r2 = textract.get_document_analysis(JobId=job_id, NextToken=tok)
                blocks.extend(r2.get("Blocks", []))
                tok = r2.get("NextToken")
            return blocks
        if r["JobStatus"] == "FAILED":
            raise RuntimeError("Textract failed")
        time.sleep(1.5)
    raise TimeoutError("Textract timed out")


def _key_values(blocks):
    by_id = {b["Id"]: b for b in blocks}

    def words(block):
        out = []
        for rel in block.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    c = by_id.get(cid, {})
                    if c.get("BlockType") == "WORD":
                        out.append(c.get("Text", ""))
        return " ".join(out)

    kv = {}
    for b in blocks:
        if b.get("BlockType") == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
            k = words(b); v = ""
            for rel in b.get("Relationships", []):
                if rel["Type"] == "VALUE":
                    for vid in rel["Ids"]:
                        v = words(by_id.get(vid, {}))
            if k:
                kv[k.strip(": ")] = v.strip()
    return kv
