"""IDP Studio — single Lambda handler with structured logging.

Routes by HTTP method + path:
  POST /v1/extract/bedrock  → handle_bedrock()
  POST /v1/extract/textract → handle_textract()
  POST /v1/agents/run       → handle_agents()
  GET  /v1/logs             → handle_logs()

Every step is logged so CloudWatch gives full visibility without needing
the Lambda console code editor (which doesn't work for zip deployments).
"""
import base64, json, logging, os, time, traceback
import boto3
from common import (
    respond, parse_body, new_run_id, write_log, mask_pii,
    check_api_key, CORS, LOG_BUCKET, INPUT_BUCKET,
)

# ── Logger setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)          # DEBUG captures everything in CloudWatch

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL  = os.environ.get("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Optional baked-in Bedrock API key (set via the `bedrock_api_key` Terraform
# variable). If present, we authenticate to Bedrock with this bearer token by
# default, so the UI doesn't have to supply one. A per-request key always wins.
MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "4096"))
ENV_BEDROCK_KEY = os.environ.get("BEDROCK_API_KEY", "").strip()
if ENV_BEDROCK_KEY:
    # Must be set BEFORE the bedrock-runtime client is created so botocore
    # wires up bearer-token auth at client construction time.
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = ENV_BEDROCK_KEY

# Log config on cold start so it shows in every new container's first log line
logger.info("=== COLD START ===")
logger.info(f"REGION={REGION}")
logger.info(f"BEDROCK_MODEL={MODEL}")
logger.info(f"LOG_BUCKET={LOG_BUCKET}")
logger.info(f"INPUT_BUCKET={INPUT_BUCKET}")
logger.info(f"bedrock_auth_default={'bearer_token(env)' if ENV_BEDROCK_KEY else 'iam_role'}")
logger.info("==================")

bedrock  = boto3.client("bedrock-runtime", region_name=REGION)
textract = boto3.client("textract",         region_name=REGION)
s3       = boto3.client("s3")
logger.info(f"boto3={boto3.__version__}")


def _bedrock_client(api_key=None):
    """Return a bedrock-runtime client.

    Auth precedence:
      1. Per-request `bedrock_api_key` (bearer token from the request body) —
         builds a fresh client so botocore uses bearer-token auth.
      2. Baked-in BEDROCK_API_KEY env var — the module-level `bedrock` client
         was already created with that bearer token at cold start.
      3. Otherwise the Lambda's IAM role (default `bedrock` client).

    Bearer-token auth bypasses the IAM / Marketplace-subscription / payment
    requirements that otherwise gate on-demand Bedrock model access. A Lambda
    execution environment handles one request at a time, so mutating the env
    var per request is safe.
    """
    if api_key:
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key
        logger.info("Using per-request Bedrock bearer token for this request")
        return boto3.client("bedrock-runtime", region_name=REGION)
    # No per-request key: reuse the module client (bearer if BEDROCK_API_KEY was
    # baked in at deploy time, otherwise IAM-auth).
    return bedrock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_body_log(body: dict) -> dict:
    """Return a version of the request body safe to log — strips PDF bytes."""
    out = {}
    for k, v in body.items():
        if k == "document_base64":
            out[k] = f"<base64 {len(v)} chars ≈ {len(v)*3//4//1024} KB>"
        elif k == "bedrock_api_key":
            out[k] = "***redacted***"          # never log the bearer token
        else:
            out[k] = v
    return out


def _sep(label: str) -> str:
    return f"{'─'*20} {label} {'─'*20}"


# ── Entry point ───────────────────────────────────────────────────────────────

def handler(event, context):
    req_id  = context.aws_request_id
    fn_name = context.function_name
    remain  = context.get_remaining_time_in_millis()

    logger.info(_sep("REQUEST"))
    logger.info(f"request_id={req_id} | function={fn_name} | remaining_ms={remain}")

    # Extract HTTP context
    req_ctx  = event.get("requestContext", {})
    http_ctx = req_ctx.get("http", {})
    method   = http_ctx.get("method", "UNKNOWN")
    path     = http_ctx.get("path",   "UNKNOWN")
    src_ip   = http_ctx.get("sourceIp", "?")

    # HTTP APIs with a NAMED stage (e.g. "prod") prefix the stage onto the
    # event path: "/prod/v1/logs". Strip it so route matching is stage-agnostic.
    stage = req_ctx.get("stage")
    if stage and stage != "$default" and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]          # "/prod/v1/logs" -> "/v1/logs"
    logger.info(f"method={method} | path={path} | stage={stage} | source_ip={src_ip}")

    # Log headers (redact x-api-key value)
    raw_headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    safe_headers = {k: ("***" if k == "x-api-key" else v) for k, v in raw_headers.items()}
    logger.debug(f"headers={json.dumps(safe_headers)}")

    # CORS preflight
    if method == "OPTIONS":
        logger.info("CORS preflight → 200 OK")
        return {"statusCode": 200, "headers": CORS, "body": ""}

    # API key check
    logger.debug("Checking API key ...")
    unauth = check_api_key(event)
    if unauth:
        logger.warning(f"API key REJECTED | path={path}")
        return unauth
    logger.debug("API key OK (or not required)")

    # Route
    logger.info(f"Routing → {method} {path}")
    if path == "/v1/extract/bedrock"  and method == "POST": return handle_bedrock(event, req_id)
    if path == "/v1/extract/textract" and method == "POST": return handle_textract(event, req_id)
    if path == "/v1/agents/run"       and method == "POST": return handle_agents(event, req_id)
    if path == "/v1/logs"             and method == "GET":  return handle_logs(event, req_id)

    logger.error(f"No route matched: {method} {path}")
    return respond(404, {"error": "not_found", "path": path, "method": method})


# ── POST /v1/extract/bedrock ──────────────────────────────────────────────────

BEDROCK_SYSTEM = (
    "You are an insurance underwriting document extraction engine. "
    "Read the attached broker document (medical questionnaire, KYC/IDP, "
    "financials, free text for a high-net-worth client). "
    "Extract the requested fields and return ONLY a JSON object that matches "
    "the provided target schema exactly — same keys and nesting. "
    "Use null or [] when a value is absent. Do not invent values. "
    "Include 'extraction_confidence' (0–1) if the field is in the schema."
)

def handle_bedrock(event, req_id=""):
    run_id = new_run_id()
    t0 = time.time()
    logger.info(_sep("BEDROCK START"))
    logger.info(f"run_id={run_id}")

    try:
        # ── Parse body
        logger.debug("Parsing request body ...")
        body     = parse_body(event)
        model_id = body.get("model", MODEL)
        filename = body.get("filename", "unknown.pdf")
        mask     = body.get("mask_pii", True)
        schema   = body.get("target_schema", "{}")
        api_key  = body.get("bedrock_api_key")

        logger.info(f"filename={filename}")
        logger.info(f"model_id={model_id}")
        logger.info(f"mask_pii={mask}")
        logger.info(f"schema_len={len(schema)} chars")
        logger.info(f"bedrock_auth={'bearer_token' if api_key else 'iam_role'}")
        logger.debug(f"safe_body={json.dumps(_safe_body_log(body))}")

        # ── Decode PDF
        logger.debug("Decoding PDF base64 ...")
        pdf_bytes = base64.b64decode(body["document_base64"])
        logger.info(f"pdf_size={len(pdf_bytes)} bytes ({len(pdf_bytes)//1024} KB)")

        # ── Call Bedrock
        client = _bedrock_client(api_key)
        logger.info(f"Calling bedrock.converse() | model={model_id} ...")
        t_api = time.time()

        resp = client.converse(
            modelId=model_id,
            system=[{"text": BEDROCK_SYSTEM}],
            messages=[{"role": "user", "content": [
                {"document": {"format": "pdf", "name": "broker_doc", "source": {"bytes": pdf_bytes}}},
                {"text": f"Target JSON schema:\n{schema}\n\nReturn only the populated JSON."},
            ]}],
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
        )

        api_ms = int((time.time() - t_api) * 1000)
        usage  = resp.get("usage", {})
        stop   = resp.get("stopReason", "?")
        logger.info(f"bedrock.converse() DONE | api_ms={api_ms}")
        logger.info(f"tokens: input={usage.get('inputTokens')} output={usage.get('outputTokens')} total={usage.get('totalTokens')}")
        logger.info(f"stop_reason={stop}")

        # ── Parse JSON from response
        raw_text = resp["output"]["message"]["content"][0]["text"]
        logger.debug(f"raw_response_preview={raw_text[:300]!r}")
        logger.debug("Parsing JSON from response ...")
        result = _parse_json(raw_text)
        logger.info(f"result_keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}")

        latency = int((time.time() - t0) * 1000)

        # ── Write log to S3
        logger.info(f"Writing run log to S3 bucket={LOG_BUCKET} ...")
        out = {
            "run_id": run_id, "capability": "bedrock", "model": model_id,
            "status": "succeeded", "latency_ms": latency, "result": result,
        }
        out["s3_key"] = write_log(
            s3, "bedrock", run_id,
            {"model": model_id, "filename": filename, "result": result},
            mask=mask,
        )
        logger.info(f"s3_key={out.get('s3_key')}")
        logger.info(f"BEDROCK SUCCESS | run_id={run_id} | total_ms={latency}")
        return respond(200, out)

    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        tb = traceback.format_exc()
        logger.error(f"BEDROCK FAILED | run_id={run_id} | ms={latency} | error={type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{tb}")
        write_log(s3, "bedrock", run_id, {"error": str(e), "traceback": tb}, mask=True)
        return respond(500, {"error": "bedrock_failed", "message": str(e), "run_id": run_id})


# ── POST /v1/extract/textract ─────────────────────────────────────────────────

def handle_textract(event, req_id=""):
    run_id = new_run_id()
    t0 = time.time()
    logger.info(_sep("TEXTRACT START"))
    logger.info(f"run_id={run_id}")

    try:
        body     = parse_body(event)
        filename = body.get("filename", "unknown.pdf")
        features = [f for f in body.get("feature_types", ["FORMS", "TABLES"])
                    if f in ("FORMS", "TABLES", "SIGNATURES")] or ["FORMS"]
        mask     = body.get("mask_pii", True)
        schema   = body.get("target_schema", "{}")
        api_key  = body.get("bedrock_api_key")

        logger.info(f"filename={filename} | features={features} | mask_pii={mask}")
        logger.info(f"schema_len={len(schema)} chars | bedrock_auth={'bearer_token' if api_key else 'iam_role'}")

        # Decode PDF
        pdf_bytes = base64.b64decode(body["document_base64"])
        logger.info(f"pdf_size={len(pdf_bytes)} bytes ({len(pdf_bytes)//1024} KB)")

        # Upload PDF to S3 (Textract reads from S3, not direct bytes)
        s3_key = f"input/{run_id}.pdf"
        logger.info(f"Uploading PDF to s3://{INPUT_BUCKET}/{s3_key} ...")
        s3.put_object(Bucket=INPUT_BUCKET, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")
        logger.info("S3 upload done")

        # Start Textract async job
        logger.info(f"Starting Textract job | features={features} ...")
        t_tx = time.time()
        job_id = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": INPUT_BUCKET, "Name": s3_key}},
            FeatureTypes=features,
        )["JobId"]
        logger.info(f"Textract job started | job_id={job_id}")

        # Poll for completion
        logger.info("Polling Textract job ...")
        blocks = _textract_poll(job_id)
        tx_ms  = int((time.time() - t_tx) * 1000)
        logger.info(f"Textract DONE | job_ms={tx_ms} | total_blocks={len(blocks)}")

        # Extract key/values
        logger.debug("Reconstructing KEY_VALUE_SET pairs ...")
        kv = _key_values(blocks)
        logger.info(f"kv_pairs_found={len(kv)}")
        if kv:
            sample = dict(list(kv.items())[:5])
            logger.debug(f"kv_sample={json.dumps(sample)}")

        # Map with Bedrock
        logger.info(f"Mapping key/values → schema via Bedrock | model={MODEL} ...")
        t_bk = time.time()
        result = _bedrock_map(kv, schema, _bedrock_client(api_key))
        bk_ms  = int((time.time() - t_bk) * 1000)
        logger.info(f"Bedrock mapping DONE | bk_ms={bk_ms}")
        logger.info(f"result_keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}")

        latency = int((time.time() - t0) * 1000)
        out = {
            "run_id": run_id, "capability": "textract",
            "service": "AWS Textract + Bedrock", "status": "succeeded",
            "latency_ms": latency, "raw_keyvalues": kv, "result": result,
        }
        logger.info(f"Writing S3 log ...")
        out["s3_key"] = write_log(
            s3, "textract", run_id,
            {"filename": filename, "raw_keyvalues": kv, "result": result},
            mask=mask,
        )
        logger.info(f"TEXTRACT SUCCESS | run_id={run_id} | total_ms={latency}")
        return respond(200, out)

    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        tb = traceback.format_exc()
        logger.error(f"TEXTRACT FAILED | run_id={run_id} | ms={latency} | error={type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{tb}")
        write_log(s3, "textract", run_id, {"error": str(e), "traceback": tb}, mask=True)
        return respond(500, {"error": "textract_failed", "message": str(e), "run_id": run_id})


# ── POST /v1/agents/run ───────────────────────────────────────────────────────

def handle_agents(event, req_id=""):
    run_id = new_run_id()
    t0 = time.time()
    logger.info(_sep("AGENTS START"))
    logger.info(f"run_id={run_id}")

    steps = []
    try:
        body   = parse_body(event)
        fname   = body.get("filename", "document.pdf")
        schema  = body.get("target_schema", "{}")
        mask    = body.get("mask_pii", True)
        api_key = body.get("bedrock_api_key")

        logger.info(f"filename={fname} | schema_len={len(schema)} | mask_pii={mask} | bedrock_auth={'bearer_token' if api_key else 'iam_role'}")

        pdf_bytes = base64.b64decode(body["document_base64"])
        logger.info(f"pdf_size={len(pdf_bytes)} bytes")

        # ── Step 1: Ingest
        logger.info("── STEP 1: Ingest ──")
        s3_key = f"input/{run_id}.pdf"
        logger.info(f"Uploading to s3://{INPUT_BUCKET}/{s3_key} ...")
        s3.put_object(Bucket=INPUT_BUCKET, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")
        steps.append(_step("ingest", "Ingest Agent", "📥", "S3",
                           {"file": fname, "size_kb": round(len(pdf_bytes) / 1024)},
                           {"stored": f"s3://{INPUT_BUCKET}/{s3_key}"}))
        logger.info("Step 1 DONE: Ingest")

        # ── Step 2: OCR
        logger.info("── STEP 2: OCR (Textract) ──")
        t_tx = time.time()
        job_id = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": INPUT_BUCKET, "Name": s3_key}},
            FeatureTypes=["FORMS", "TABLES"])["JobId"]
        logger.info(f"Textract job_id={job_id} | polling ...")
        blocks = _textract_poll(job_id)
        kv     = _key_values(blocks)
        tx_ms  = int((time.time() - t_tx) * 1000)
        logger.info(f"Step 2 DONE: OCR | job_ms={tx_ms} | blocks={len(blocks)} | kv_pairs={len(kv)}")
        steps.append(_step("ocr", "OCR Agent", "🔎", "AWS Textract",
                           {"s3_key": s3_key}, {"kv_pairs": len(kv), "blocks": len(blocks)}))

        # ── Step 3: PII Mask
        logger.info("── STEP 3: PII Mask ──")
        masked     = mask_pii(kv)
        pii_fields = [k for k, v in kv.items() if v != masked.get(k)]
        logger.info(f"Step 3 DONE: PII | masked_fields={pii_fields}")
        steps.append(_step("pii", "PII Masking Agent", "🛡️", "regex",
                           {"kv_pairs": len(kv)},
                           {"pii_found": len(pii_fields), "masked_fields": pii_fields}))

        # ── Step 4: Extract
        logger.info(f"── STEP 4: Extract (Bedrock model={MODEL}) ──")
        t_bk = time.time()
        result = _bedrock_map(masked, schema, _bedrock_client(api_key))
        bk_ms  = int((time.time() - t_bk) * 1000)
        logger.info(f"Step 4 DONE: Extract | bk_ms={bk_ms} | result_keys={list(result.keys()) if isinstance(result, dict) else '?'}")
        steps.append(_step("extract", "Extraction Agent", "🧠", "AWS Bedrock",
                           {"masked": True}, result))

        # ── Step 5: Validate
        logger.info("── STEP 5: Validate ──")
        missing = _validate(result, schema)
        logger.info(f"Step 5 DONE: Validate | missing_fields={missing}")
        steps.append(_step("validate", "Validation Agent", "✅", "Schema check",
                           {"against": "target_schema"},
                           {"valid": not missing, "missing_required": missing}))

        latency = int((time.time() - t0) * 1000)
        out = {"run_id": run_id, "capability": "agents", "status": "succeeded",
               "latency_ms": latency, "steps": steps, "result": result}
        logger.info(f"Writing S3 log ...")
        out["s3_key"] = write_log(s3, "agents", run_id,
                                  {"filename": fname, "steps": steps, "result": result},
                                  mask=mask)
        logger.info(f"AGENTS SUCCESS | run_id={run_id} | total_ms={latency}")
        return respond(200, out)

    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        tb = traceback.format_exc()
        logger.error(f"AGENTS FAILED | run_id={run_id} | step={len(steps)} | ms={latency}")
        logger.error(f"error={type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{tb}")
        write_log(s3, "agents", run_id, {"error": str(e), "traceback": tb, "steps": steps}, mask=True)
        return respond(500, {"error": "agents_failed", "message": str(e),
                             "run_id": run_id, "steps": steps})


# ── GET /v1/logs ──────────────────────────────────────────────────────────────

def handle_logs(event, req_id=""):
    logger.info(_sep("LOGS START"))
    try:
        qs         = event.get("queryStringParameters") or {}
        capability = qs.get("capability")
        limit      = min(int(qs.get("limit", 50)), 200)
        prefix     = f"logs/{capability}/" if capability else "logs/"
        logger.info(f"prefix={prefix} | limit={limit}")

        logger.debug(f"Listing objects in s3://{LOG_BUCKET}/{prefix} ...")
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=LOG_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append((obj["LastModified"], obj["Key"]))
        keys.sort(reverse=True)
        logger.info(f"total_objects_found={len(keys)} | returning={min(len(keys), limit)}")

        items = []
        for _, key in keys[:limit]:
            try:
                entry = json.loads(s3.get_object(Bucket=LOG_BUCKET, Key=key)["Body"].read())
                d = entry.get("detail", {})
                items.append({
                    "timestamp":  entry.get("timestamp"),
                    "run_id":     entry.get("run_id"),
                    "capability": entry.get("capability"),
                    "model":      d.get("model") or d.get("service") or "—",
                    "status":     "error" if "error" in d else "succeeded",
                    "latency_ms": d.get("latency_ms"),
                    "s3_key":     key,
                    "detail":     entry,
                })
            except Exception as ex:
                logger.warning(f"Skipping log object key={key} | error={ex}")

        logger.info(f"LOGS SUCCESS | items_returned={len(items)}")
        return respond(200, {"items": items})

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"LOGS FAILED | error={type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{tb}")
        return respond(500, {"error": "logs_failed", "message": str(e)})


# ── Shared helpers ────────────────────────────────────────────────────────────

def _step(sid, title, icon, svc, inp, outp):
    return {"id": sid, "title": title, "icon": icon, "svc": svc, "input": inp, "output": outp}


def _textract_poll(job_id, max_polls=20):
    for attempt in range(1, max_polls + 1):
        r      = textract.get_document_analysis(JobId=job_id)
        status = r["JobStatus"]
        logger.debug(f"Textract poll {attempt}/{max_polls} | status={status}")
        if status == "SUCCEEDED":
            blocks = list(r.get("Blocks", []))
            tok = r.get("NextToken")
            while tok:
                r2 = textract.get_document_analysis(JobId=job_id, NextToken=tok)
                blocks.extend(r2.get("Blocks", []))
                tok = r2.get("NextToken")
            logger.info(f"Textract SUCCEEDED after {attempt} polls | total_blocks={len(blocks)}")
            return blocks
        if status == "FAILED":
            msg = r.get("StatusMessage", "no message")
            logger.error(f"Textract job FAILED | job_id={job_id} | message={msg}")
            raise RuntimeError(f"Textract job failed: {msg}")
        time.sleep(1.5)
    raise TimeoutError(f"Textract job {job_id} did not finish after {max_polls} polls")


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
                    elif c.get("BlockType") == "SELECTION_ELEMENT" and c.get("SelectionStatus") == "SELECTED":
                        out.append("[X]")
        return " ".join(out)

    kv = {}
    for b in blocks:
        if b.get("BlockType") == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
            key_text = words(b)
            val_text = ""
            for rel in b.get("Relationships", []):
                if rel["Type"] == "VALUE":
                    for vid in rel["Ids"]:
                        val_text = words(by_id.get(vid, {}))
            if key_text:
                kv[key_text.strip(": ")] = val_text.strip()
    return kv


def _bedrock_map(kv, schema, client=None):
    client = client or bedrock
    prompt = (
        "Map these key/value pairs from an insurance document to the target JSON schema. "
        "Return only valid JSON. Use null or [] for missing values.\n\n"
        f"Key/Values:\n{json.dumps(kv, indent=2)}\n\nTarget schema:\n{schema}"
    )
    logger.debug(f"bedrock_map prompt_len={len(prompt)} chars")
    resp = client.converse(
        modelId=MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
    )
    usage = resp.get("usage", {})
    logger.debug(f"bedrock_map tokens: in={usage.get('inputTokens')} out={usage.get('outputTokens')}")
    return _parse_json(resp["output"]["message"]["content"][0]["text"])


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip("` \n")
    try:
        return json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            logger.debug("JSON parse: used bracket extraction fallback")
            return json.loads(text[s:e + 1])
        raise


def _validate(result, schema_str):
    try:
        schema = json.loads(schema_str)
    except Exception:
        logger.warning("Could not parse schema_str as JSON for validation")
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
