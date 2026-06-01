"""IDP Studio — single Lambda handler with structured logging.

Routes by HTTP method + path:
  POST /v1/extract/bedrock  → handle_bedrock()
  POST /v1/extract/textract → handle_textract()
  POST /v1/agents/run       → handle_agents()
  GET  /v1/logs             → handle_logs()

Every step is logged so CloudWatch gives full visibility without needing
the Lambda console code editor (which doesn't work for zip deployments).
"""
import base64, difflib, json, logging, os, re, time, traceback
import urllib.request, urllib.error
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

bedrock    = boto3.client("bedrock-runtime",  region_name=REGION)
textract   = boto3.client("textract",         region_name=REGION)
comprehend = boto3.client("comprehend",       region_name=REGION)
cmedical   = boto3.client("comprehendmedical", region_name=REGION)
s3         = boto3.client("s3")
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
        elif k == "llm" and isinstance(v, dict):
            out[k] = {**v, "api_key": "***redacted***" if v.get("api_key") else ""}
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
    if path == "/v1/extract/bedrock"   and method == "POST": return handle_bedrock(event, req_id)
    if path == "/v1/extract/textract"  and method == "POST": return handle_textract(event, req_id)
    if path == "/v1/agents/run"        and method == "POST": return handle_agents(event, req_id)
    if path == "/v1/agents/run-external" and method == "POST": return handle_agents_external(event, req_id)
    if path == "/v1/agents/prepare"    and method == "POST": return handle_prepare(event, req_id)
    if path == "/v1/agents/extract"    and method == "POST": return handle_extract(event, req_id)
    if path == "/v1/logs"              and method == "GET":  return handle_logs(event, req_id)

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
        mask       = body.get("mask_pii", True)
        schema_str = body.get("target_schema")   # optional — enables best-effort mapping

        logger.info(f"filename={filename} | features={features} | mask_pii={mask} | map_to_schema={bool(schema_str)} | mode=textract-only (no LLM)")

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

        # Extract key/values + tables — NO LLM. Return Textract's data as JSON.
        logger.debug("Reconstructing KEY_VALUE_SET pairs + tables ...")
        kv = _key_values(blocks)
        tables = _tables(blocks) if "TABLES" in features else []
        logger.info(f"kv_pairs_found={len(kv)} | tables_found={len(tables)}")

        result = {
            "source": "AWS Textract (no LLM)",
            "key_values": kv,
            "tables": tables,
            "summary": {"kv_pairs": len(kv), "tables": len(tables), "blocks": len(blocks)},
        }

        # Optional: best-effort, no-LLM mapping of key/values onto the target schema
        if schema_str:
            mapped, map_stats = _flatten_to_schema(kv, schema_str)
            if mapped is not None:
                result["target_json"] = mapped
                result["mapping"] = map_stats
                logger.info(f"schema mapping: matched {map_stats.get('matched')}/{map_stats.get('total')} fields")
            else:
                result["mapping"] = map_stats  # carries the error

        latency = int((time.time() - t0) * 1000)
        out = {
            "run_id": run_id, "capability": "textract",
            "service": "AWS Textract (forms + tables → JSON, no LLM)", "status": "succeeded",
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

        # ── Step 3: PII + PHI Mask (AWS Comprehend / Comprehend Medical)
        logger.info("── STEP 3: PII/PHI Mask (Comprehend) ──")
        detect_phi = body.get("detect_phi", True)
        try:
            masked, _mm, cstats = _comprehend_mask(kv, do_phi=detect_phi)
        except Exception as ce:
            logger.warning(f"Comprehend unavailable, falling back to regex: {ce}")
            masked = mask_pii(kv)
            cstats = {"engine": "regex (Comprehend unavailable)", "pii_entities": 0,
                      "phi_entities": 0, "types": []}
        masked_fields = [k for k in kv if kv[k] != masked.get(k)]
        logger.info(f"Step 3 DONE: PII/PHI | engine={cstats['engine']} | "
                    f"pii={cstats['pii_entities']} phi={cstats['phi_entities']} | masked_fields={len(masked_fields)}")
        steps.append(_step("pii", "PII/PHI Masking Agent", "🛡️", cstats["engine"],
                           {"kv_pairs": len(kv), "detect_phi": detect_phi},
                           {"pii_entities": cstats["pii_entities"], "phi_entities": cstats["phi_entities"],
                            "entity_types": cstats["types"], "masked_fields": masked_fields}))

        # ── Step 4: Extract (Bedrock) — isolated so steps 1-3 are preserved if it fails
        logger.info(f"── STEP 4: Extract (Bedrock model={MODEL}) ──")
        try:
            t_bk = time.time()
            result = _bedrock_map(masked, schema, _bedrock_client(api_key))
            bk_ms  = int((time.time() - t_bk) * 1000)
            logger.info(f"Step 4 DONE: Extract | bk_ms={bk_ms}")
            steps.append(_step("extract", "Extraction Agent", "🧠", "AWS Bedrock",
                               {"masked": True}, result))
        except Exception as be:
            logger.error(f"Step 4 FAILED: Bedrock | error={type(be).__name__}: {be}")
            estep = _step("extract", "Extraction Agent", "🧠", "AWS Bedrock", {"masked": True}, None)
            estep["status"] = "error"
            estep["error"]  = f"{type(be).__name__}: {be}"
            steps.append(estep)
            latency = int((time.time() - t0) * 1000)
            out = {
                "run_id": run_id, "capability": "agents",
                "status": "failed_at_extract", "failed_step": "extract",
                "latency_ms": latency, "steps": steps, "result": None,
                "ocr_keyvalues": kv, "masked_keyvalues": masked,
                "error": "bedrock_failed", "message": str(be),
            }
            out["s3_key"] = write_log(s3, "agents", run_id,
                                      {"filename": fname, "steps": steps, "error": str(be)},
                                      mask=mask)
            logger.info(f"AGENTS PARTIAL (steps 1-3 ok, step 4 Bedrock failed) | run_id={run_id}")
            return respond(200, out)

        # ── Step 5: Validate (only reached if extract succeeded)
        logger.info("── STEP 5: Validate ──")
        missing = _validate(result, schema)
        logger.info(f"Step 5 DONE: Validate | missing_fields={missing}")
        steps.append(_step("validate", "Validation Agent", "✅", "Schema check",
                           {"against": "target_schema"},
                           {"valid": not missing, "missing_required": missing}))

        latency = int((time.time() - t0) * 1000)
        out = {"run_id": run_id, "capability": "agents", "status": "succeeded",
               "latency_ms": latency, "steps": steps, "result": result,
               "ocr_keyvalues": kv, "masked_keyvalues": masked}
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


# ── POST /v1/agents/run-external ──────────────────────────────────────────────
# Clone of the agentic pipeline, but step 4 calls a caller-supplied EXTERNAL LLM
# (Google Gemini / Anthropic / OpenAI-compatible) instead of AWS Bedrock.
# Steps: 1 Ingest → 2 OCR (Textract) → 3 PII/PHI mask (Comprehend) →
#        4 Extract (your LLM) → 5 Validate → 6 Finalize/deliver.

def handle_agents_external(event, req_id=""):
    run_id = new_run_id()
    t0 = time.time()
    logger.info(_sep("AGENTS-EXTERNAL START"))
    logger.info(f"run_id={run_id}")

    steps = []
    try:
        body       = parse_body(event)
        fname      = body.get("filename", "document.pdf")
        schema     = body.get("target_schema", "{}")
        mask       = body.get("mask_pii", True)
        detect_phi = body.get("detect_phi", True)
        llm        = body.get("llm") or {}
        provider   = (llm.get("provider") or "?")
        model      = llm.get("model", "")
        logger.info(f"filename={fname} | provider={provider} | model={model} | detect_phi={detect_phi}")

        pdf_bytes = base64.b64decode(body["document_base64"])

        # ── Step 1: Ingest
        s3_key = f"input/{run_id}.pdf"
        s3.put_object(Bucket=INPUT_BUCKET, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")
        steps.append(_step("ingest", "Ingest Agent", "📥", "S3",
                           {"file": fname, "size_kb": round(len(pdf_bytes) / 1024)},
                           {"stored": f"s3://{INPUT_BUCKET}/{s3_key}"}))

        # ── Step 2: OCR (Textract)
        job_id = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": INPUT_BUCKET, "Name": s3_key}},
            FeatureTypes=["FORMS", "TABLES"])["JobId"]
        blocks = _textract_poll(job_id)
        kv = _key_values(blocks)
        steps.append(_step("ocr", "OCR Agent", "🔎", "AWS Textract",
                           {"s3_key": s3_key}, {"kv_pairs": len(kv), "blocks": len(blocks)}))

        # ── Step 3: PII/PHI mask (Comprehend) — UNIQUE tokens so we can reverse it
        try:
            masked, mask_map, cstats = _comprehend_mask(kv, do_phi=detect_phi, unique=True)
        except Exception as ce:
            logger.warning(f"Comprehend unavailable, regex fallback: {ce}")
            masked, mask_map = mask_pii(kv), {}
            cstats = {"engine": "regex (Comprehend unavailable)", "pii_entities": 0,
                      "phi_entities": 0, "types": []}
        masked_fields = [k for k in kv if kv[k] != masked.get(k)]
        steps.append(_step("pii", "PII/PHI Masking Agent", "🛡️", cstats["engine"],
                           {"kv_pairs": len(kv), "detect_phi": detect_phi},
                           {"pii_entities": cstats["pii_entities"], "phi_entities": cstats["phi_entities"],
                            "entity_types": cstats["types"], "masked_fields": masked_fields}))

        # ── Step 4: Extract via the caller's EXTERNAL LLM (masked input)
        logger.info(f"── STEP 4: Extract (external LLM provider={provider}) ──")
        try:
            prompt = _build_extract_prompt(body.get("prompt"), masked, schema)
            masked_result, prov = _call_external_llm(llm, prompt)
            steps.append(_step("extract", f"Extraction Agent · {provider}", "🧠",
                               f"External LLM: {provider}/{model}",
                               {"provider": provider, "model": model}, masked_result))
        except Exception as le:
            logger.error(f"Step 4 FAILED: external LLM | {type(le).__name__}: {le}")
            estep = _step("extract", f"Extraction Agent · {provider}", "🧠",
                          f"External LLM: {provider}/{model}",
                          {"provider": provider, "model": model}, None)
            estep["status"] = "error"
            estep["error"]  = f"{type(le).__name__}: {le}"
            steps.append(estep)
            latency = int((time.time() - t0) * 1000)
            out = {"run_id": run_id, "capability": "agents-external",
                   "status": "failed_at_extract", "failed_step": "extract",
                   "latency_ms": latency, "steps": steps, "result": None,
                   "ocr_keyvalues": kv, "masked_keyvalues": masked,
                   "error": "llm_failed", "message": str(le)}
            out["s3_key"] = write_log(s3, "agents-external", run_id,
                                      {"filename": fname, "provider": provider, "model": model,
                                       "steps": steps, "error": str(le)}, mask=mask)
            return respond(200, out)

        # ── Step 5: Unmask — replace [PII_n] tokens with original values
        result = _unmask(masked_result, mask_map)
        steps.append(_step("unmask", "Unmasking Agent", "🔓", "Restore original PII/PHI",
                           {"tokens": len(mask_map)}, {"restored": len(mask_map)}))

        # ── Step 6: Finalize / validate / deliver
        missing = _validate(result, schema)
        final = {"target_json": result, "provider": provider, "model": model,
                 "valid": not missing, "missing_required": missing,
                 "pii_entities": cstats["pii_entities"], "phi_entities": cstats["phi_entities"]}
        steps.append(_step("finalize", "Finalize Agent", "📦", "Validate + deliver",
                           {"fields": len(result) if isinstance(result, dict) else 0},
                           {"delivered": True, "schema_valid": not missing, "missing_required": missing}))

        latency = int((time.time() - t0) * 1000)
        out = {"run_id": run_id, "capability": "agents-external", "status": "succeeded",
               "latency_ms": latency, "steps": steps, "result": result, "final": final,
               "ocr_keyvalues": kv, "masked_keyvalues": masked}
        out["s3_key"] = write_log(s3, "agents-external", run_id,
                                  {"filename": fname, "provider": provider, "model": model,
                                   "steps": steps, "result": result}, mask=mask)
        logger.info(f"AGENTS-EXTERNAL SUCCESS | run_id={run_id} | provider={provider} | ms={latency}")
        return respond(200, out)

    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        tb = traceback.format_exc()
        logger.error(f"AGENTS-EXTERNAL FAILED | run_id={run_id} | {type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{tb}")
        write_log(s3, "agents-external", run_id, {"error": str(e), "traceback": tb, "steps": steps}, mask=True)
        return respond(500, {"error": "agents_external_failed", "message": str(e),
                             "run_id": run_id, "steps": steps})


# ── POST /v1/agents/prepare  (Tab 4 part 1: steps 1-3) ────────────────────────
# Ingest → Textract OCR → Comprehend PII/PHI mask (UNIQUE tokens). Stores the
# original kv + masked kv + reversible mask_map in S3 under the run_id so the
# follow-up /extract call can run the LLM and then un-mask. Returns the OCR JSON
# (so the user can see/copy it) and the masked JSON.

def handle_prepare(event, req_id=""):
    run_id = new_run_id()
    t0 = time.time()
    logger.info(_sep("PREPARE START")); logger.info(f"run_id={run_id}")
    steps = []
    try:
        body       = parse_body(event)
        fname      = body.get("filename", "document.pdf")
        detect_phi = body.get("detect_phi", True)
        pdf_bytes  = base64.b64decode(body["document_base64"])

        # Step 1: Ingest
        s3_key = f"input/{run_id}.pdf"
        s3.put_object(Bucket=INPUT_BUCKET, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")
        steps.append(_step("ingest", "Ingest Agent", "📥", "S3",
                           {"file": fname, "size_kb": round(len(pdf_bytes) / 1024)},
                           {"stored": f"s3://{INPUT_BUCKET}/{s3_key}"}))

        # Step 2: OCR
        job_id = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": INPUT_BUCKET, "Name": s3_key}},
            FeatureTypes=["FORMS", "TABLES"])["JobId"]
        blocks = _textract_poll(job_id)
        kv = _key_values(blocks)
        steps.append(_step("ocr", "OCR Agent", "🔎", "AWS Textract",
                           {"s3_key": s3_key}, {"kv_pairs": len(kv), "blocks": len(blocks)}))

        # Step 3: PII/PHI mask (unique tokens, reversible)
        try:
            masked, mask_map, cstats = _comprehend_mask(kv, do_phi=detect_phi, unique=True)
        except Exception as ce:
            logger.warning(f"Comprehend unavailable, regex fallback: {ce}")
            masked, mask_map = mask_pii(kv), {}
            cstats = {"engine": "regex (Comprehend unavailable)", "pii_entities": 0,
                      "phi_entities": 0, "types": []}
        steps.append(_step("pii", "PII/PHI Masking Agent", "🛡️", cstats["engine"],
                           {"kv_pairs": len(kv), "detect_phi": detect_phi},
                           {"pii_entities": cstats["pii_entities"], "phi_entities": cstats["phi_entities"],
                            "entity_types": cstats["types"]}))

        # Persist the reversible context for /extract (expires with the bucket, 1 day)
        s3.put_object(Bucket=INPUT_BUCKET, Key=f"prepare/{run_id}.json",
                      Body=json.dumps({"ocr": kv, "masked": masked, "mask_map": mask_map}).encode("utf-8"),
                      ContentType="application/json")

        latency = int((time.time() - t0) * 1000)
        out = {"run_id": run_id, "capability": "prepare", "status": "succeeded",
               "latency_ms": latency, "steps": steps,
               "ocr_keyvalues": kv, "masked_keyvalues": masked,
               "entities": {"pii": cstats["pii_entities"], "phi": cstats["phi_entities"],
                            "types": cstats["types"], "tokens": len(mask_map)}}
        write_log(s3, "agents-external", run_id,
                  {"phase": "prepare", "filename": fname, "steps": steps}, mask=True)
        logger.info(f"PREPARE SUCCESS | run_id={run_id} | kv={len(kv)} | tokens={len(mask_map)}")
        return respond(200, out)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"PREPARE FAILED | {type(e).__name__}: {e}\n{tb}")
        return respond(500, {"error": "prepare_failed", "message": str(e), "run_id": run_id, "steps": steps})


# ── POST /v1/agents/extract  (Tab 4 part 2: steps 4-6) ────────────────────────
# Loads the prepared masked kv + mask_map by run_id, runs the caller's LLM on
# the MASKED data, then UN-MASKS the result so the final JSON has real values.

def handle_extract(event, req_id=""):
    t0 = time.time()
    logger.info(_sep("EXTRACT START"))
    steps = []
    try:
        body     = parse_body(event)
        run_id   = body.get("run_id")
        schema   = body.get("target_schema", "{}")
        llm      = body.get("llm") or {}
        provider = (llm.get("provider") or "?")
        model    = llm.get("model", "")
        if not run_id:
            return respond(400, {"error": "missing_run_id", "message": "Run 'prepare' (steps 1-3) first."})

        # Load the prepared, reversible context
        try:
            ctx = json.loads(s3.get_object(Bucket=INPUT_BUCKET, Key=f"prepare/{run_id}.json")["Body"].read())
        except Exception:
            return respond(404, {"error": "prepare_not_found",
                                 "message": "No prepared data for this run_id (it may have expired). Re-run steps 1-3."})
        masked, mask_map = ctx.get("masked", {}), ctx.get("mask_map", {})
        logger.info(f"run_id={run_id} | provider={provider} | model={model} | tokens={len(mask_map)}")

        # Step 4: Extract via external LLM (masked input)
        try:
            prompt = _build_extract_prompt(body.get("prompt"), masked, schema)
            masked_result, prov = _call_external_llm(llm, prompt)
            steps.append(_step("extract", f"Extraction Agent · {provider}", "🧠",
                               f"External LLM: {provider}/{model}",
                               {"provider": provider, "model": model}, masked_result))
        except Exception as le:
            logger.error(f"Step 4 FAILED: external LLM | {type(le).__name__}: {le}")
            estep = _step("extract", f"Extraction Agent · {provider}", "🧠",
                          f"External LLM: {provider}/{model}", {"provider": provider, "model": model}, None)
            estep["status"] = "error"; estep["error"] = f"{type(le).__name__}: {le}"
            steps.append(estep)
            out = {"run_id": run_id, "capability": "agents-external", "status": "failed_at_extract",
                   "failed_step": "extract", "steps": steps, "result": None,
                   "error": "llm_failed", "message": str(le)}
            write_log(s3, "agents-external", run_id, {"phase": "extract", "provider": provider,
                      "model": model, "error": str(le)}, mask=True)
            return respond(200, out)

        # Step 5: Unmask
        result = _unmask(masked_result, mask_map)
        steps.append(_step("unmask", "Unmasking Agent", "🔓", "Restore original PII/PHI",
                           {"tokens": len(mask_map)}, {"restored": len(mask_map)}))

        # Step 6: Finalize / validate
        missing = _validate(result, schema)
        steps.append(_step("finalize", "Finalize Agent", "📦", "Validate + deliver",
                           {"fields": len(result) if isinstance(result, dict) else 0},
                           {"delivered": True, "schema_valid": not missing, "missing_required": missing}))

        latency = int((time.time() - t0) * 1000)
        final = {"target_json": result, "provider": provider, "model": model,
                 "valid": not missing, "missing_required": missing}
        out = {"run_id": run_id, "capability": "agents-external", "status": "succeeded",
               "latency_ms": latency, "steps": steps, "result": result, "final": final,
               "masked_result": masked_result}
        out["s3_key"] = write_log(s3, "agents-external", run_id,
                                  {"phase": "extract", "provider": provider, "model": model,
                                   "steps": steps, "result": result}, mask=True)
        logger.info(f"EXTRACT SUCCESS | run_id={run_id} | provider={provider} | ms={latency}")
        return respond(200, out)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"EXTRACT FAILED | {type(e).__name__}: {e}\n{tb}")
        return respond(500, {"error": "extract_failed", "message": str(e), "steps": steps})


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


def _tables(blocks):
    """Reconstruct TABLE blocks into a list of tables, each a list of row arrays."""
    by_id = {b["Id"]: b for b in blocks}

    def cell_text(cell):
        out = []
        for rel in cell.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    c = by_id.get(cid, {})
                    if c.get("BlockType") == "WORD":
                        out.append(c.get("Text", ""))
                    elif c.get("BlockType") == "SELECTION_ELEMENT" and c.get("SelectionStatus") == "SELECTED":
                        out.append("[X]")
        return " ".join(out)

    tables = []
    for b in blocks:
        if b.get("BlockType") == "TABLE":
            cells, max_r, max_c = {}, 0, 0
            for rel in b.get("Relationships", []):
                if rel["Type"] in ("CHILD", "TABLE_FOOTER", "TABLE_TITLE"):
                    for cid in rel["Ids"]:
                        c = by_id.get(cid, {})
                        if c.get("BlockType") == "CELL":
                            r, col = c.get("RowIndex", 0), c.get("ColumnIndex", 0)
                            cells[(r, col)] = cell_text(c)
                            max_r, max_c = max(max_r, r), max(max_c, col)
            rows = [[cells.get((r, col), "") for col in range(1, max_c + 1)]
                    for r in range(1, max_r + 1)]
            if rows:
                tables.append(rows)
    return tables


# Field-LABEL hints: if the Textract key matches, the whole VALUE is treated as
# PII/PHI. This catches form fields that Comprehend misses without sentence
# context (e.g. "First name: Isha"). Aggressive on purpose — values are restored
# on unmask, so over-masking is safe and keeps PII away from the external LLM.
_KEY_PII = [
    (re.compile(r"(first|last|middle|maiden|given|sur|full|legal)\s*name|\bname\b|applicant|insured|beneficiary|spouse|dependent|nominee|physician|\bbroker\b|employer", re.I), "NAME"),
    (re.compile(r"date of birth|d\.?o\.?b|birth\s*date|\bdob\b", re.I), "DOB"),
    (re.compile(r"e-?mail", re.I), "EMAIL"),
    (re.compile(r"phone|mobile|cell|\bfax\b|telephone|contact\s*(no|number)", re.I), "PHONE"),
    (re.compile(r"address|street|residence|residential|postal|post\s*code|\bzip\b|place of birth", re.I), "ADDRESS"),
    (re.compile(r"\bssn\b|\bsin\b|social security|national\s*id|nric|aadhaar|\bpan\b|tax\s*id", re.I), "GOV_ID"),
    (re.compile(r"passport|driver'?s?\s*licen|licen[cs]e\s*(no|number)", re.I), "ID"),
    (re.compile(r"(policy|member|account|customer|certificate|group|plan|reference)\s*(no|number|id|#|ref)", re.I), "ACCOUNT_ID"),
]

# Always-on value regex (emails / phones / long numeric ids), independent of Comprehend.
_VALUE_REGEX = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "EMAIL"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "PHONE"),
    (re.compile(r"\b\d{6,}\b"), "ID"),
]


def _comprehend_mask(kv, do_phi=True, unique=False):
    """Mask PII/PHI in Textract key/values. Four layers (so misses are rare):
      1. Amazon Comprehend DetectPiiEntities — fed the field LABEL as context.
      2. Amazon Comprehend Medical DetectPHI (if do_phi) — optional.
      3. Field-label heuristic (_KEY_PII) — masks the whole value when the key
         name indicates PII (catches names/DOB/etc. Comprehend misses).
      4. Always-on regex (_VALUE_REGEX) — emails / phones / long ids.
    Layers 1-2 are best-effort (skipped on error); 3-4 always run, so masking
    works even if Comprehend is unavailable.

    Returns (masked_kv, mask_map, stats).
      - unique=False: generic [TYPE] labels (mask_map empty) — Tab 3.
      - unique=True : unique [PII_n] tokens + reversible mask_map — Tab 4.
    Cost-minimising: one Comprehend call + one Comprehend Medical call per doc.
    """
    keys = list(kv.keys())
    # "key: value\n" blob → Comprehend sees the label context, but we only ever
    # mask inside the VALUE sub-range (recorded in `ranges`).
    blob, ranges = "", []
    for k in keys:
        v = str(kv.get(k) or "")
        blob += f"{k}: "
        start = len(blob)
        blob += v
        ranges.append((k, start, len(blob)))
        blob += "\n"

    stats = {"engine": "Comprehend PII + key/regex", "pii_entities": 0, "phi_entities": 0, "types": []}
    if not blob.strip():
        return dict(kv), {}, stats

    spans, types = [], set()

    # 1) Amazon Comprehend PII (context-aware) — best-effort
    try:
        r = comprehend.detect_pii_entities(Text=blob[:99000], LanguageCode="en")
        for e in r.get("Entities", []):
            spans.append((e["BeginOffset"], e["EndOffset"], e["Type"]))
            types.add(e["Type"]); stats["pii_entities"] += 1
    except Exception as ex:
        logger.warning(f"Comprehend PII skipped: {ex}")

    # 2) Amazon Comprehend Medical PHI — optional, best-effort
    if do_phi:
        try:
            mr = cmedical.detect_phi(Text=blob[:19000])
            for e in mr.get("Entities", []):
                lbl = "PHI_" + e.get("Type", "ENTITY")
                spans.append((e["BeginOffset"], e["EndOffset"], lbl))
                types.add(lbl); stats["phi_entities"] += 1
            stats["engine"] = "Comprehend PII + Comprehend Medical PHI + key/regex"
        except Exception as ex:
            logger.warning(f"Comprehend Medical PHI skipped: {ex}")

    # 3) Field-label heuristic — mask the WHOLE value when the key looks like PII
    for (k, start, end) in ranges:
        if end > start:
            for rx, lbl in _KEY_PII:
                if rx.search(k):
                    spans.append((start, end, lbl)); types.add(lbl); break

    # 4) Always-on regex over the blob (emails / phones / long ids)
    for rx, lbl in _VALUE_REGEX:
        for m in rx.finditer(blob):
            spans.append((m.start(), m.end(), lbl)); types.add(lbl)

    # keep only spans fully inside a value range (never mask the key label itself)
    spans = [(b, e, t) for (b, e, t) in spans
             if any(s <= b and e <= en for (_k, s, en) in ranges)]
    stats["types"] = sorted(types)

    # Merge overlapping spans — greedy, keep earliest/longest.
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    merged, last_end = [], -1
    for b, e, t in spans:
        if b >= last_end:
            merged.append((b, e, t)); last_end = e

    # Assign a stable token per span (unique mode) + build the reverse map.
    mask_map = {}
    span_tokens = []  # (begin, end, replacement)
    for i, (b, e, t) in enumerate(merged, start=1):
        if unique:
            token = f"[PII_{i}]"
            mask_map[token] = {"value": blob[b:e], "type": t}
            span_tokens.append((b, e, token))
        else:
            span_tokens.append((b, e, f"[{t}]"))

    # Re-mask each value independently using offsets local to that value.
    masked_kv = {}
    for (k, vstart, vend) in ranges:
        v = blob[vstart:vend]
        local = [(b - vstart, e - vstart, rep) for (b, e, rep) in span_tokens if b >= vstart and e <= vend]
        if local:
            out = v
            for b, e, rep in sorted(local, key=lambda x: -x[0]):
                out = out[:b] + rep + out[e:]
            masked_kv[k] = out
        else:
            masked_kv[k] = kv[k]
    stats["masked_fields_count"] = sum(1 for k in kv if masked_kv.get(k) != kv.get(k))
    return masked_kv, mask_map, stats


def _unmask(obj, mask_map):
    """Recursively replace [PII_n] tokens in a JSON value with their originals."""
    if not mask_map:
        return obj
    if isinstance(obj, str):
        s = obj
        for tok, info in mask_map.items():
            if tok in s:
                s = s.replace(tok, str(info.get("value", "")))
        return s
    if isinstance(obj, dict):
        return {k: _unmask(v, mask_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_unmask(v, mask_map) for v in obj]
    return obj


def _norm(s):
    """Normalise a key for fuzzy comparison: lowercase, alnum tokens only."""
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _flatten_to_schema(kv, schema_str, threshold=0.55):
    """Best-effort, NO-LLM mapping of Textract key/values onto the target schema.

    Each scalar leaf key in the schema is matched to the closest Textract key by
    name similarity (difflib ratio + token overlap + substring). Returns
    (mapped_dict, stats). Purely deterministic — no model involved.
    """
    try:
        schema = json.loads(schema_str)
    except Exception:
        return None, {"error": "target_schema is not valid JSON"}

    kv_items = [(_norm(k), k) for k in kv.keys() if _norm(k)]
    stats = {"matched": 0, "total": 0, "method": "fuzzy key match (difflib, no LLM)",
             "threshold": threshold}

    def best(key):
        tn = _norm(key)
        tt = set(tn.split())
        best_key, best_score = None, 0.0
        for nk, ok in kv_items:
            kt = set(nk.split())
            ratio   = difflib.SequenceMatcher(None, tn, nk).ratio()
            overlap = (len(tt & kt) / len(tt)) if tt else 0.0
            sub     = 1.0 if (tn and (tn in nk or nk in tn)) else 0.0
            score   = max(ratio, overlap * 0.9, sub * 0.85)
            if score > best_score:
                best_score, best_key = score, ok
        return best_key, best_score

    def fill(key, node):
        if isinstance(node, dict):
            return {k: fill(k, v) for k, v in node.items()}
        if isinstance(node, list):
            return node if node else []
        stats["total"] += 1
        mk, score = best(key)
        if mk and score >= threshold and kv.get(mk, "") != "":
            stats["matched"] += 1
            return kv[mk]
        return None

    mapped = ({k: fill(k, v) for k, v in schema.items()}
              if isinstance(schema, dict) else fill("root", schema))
    return mapped, stats


DEFAULT_INSURANCE_PROMPT = (
    "You are an expert insurance underwriting data-extraction assistant. The input is "
    "masked key/value text extracted by OCR from an insurance underwriting document "
    "(application form, medical questionnaire, KYC/identity page, or financial statement "
    "for an often high-net-worth applicant). Extract the requested fields into the target "
    "JSON schema.\n"
    "Rules:\n"
    "- Return ONLY one JSON object that exactly matches the target schema (same keys and nesting).\n"
    "- PRESERVE any placeholder tokens such as [PII_3] or [PII_12] EXACTLY as they appear in the "
    "source values — do not alter, translate, summarise, split or drop them (they are substituted "
    "with real values afterwards).\n"
    "- Use null for missing scalar fields and [] for missing arrays. Do not invent values.\n"
    "- Treat [X] / 'checked' as the selected option for tick-box / yes-no fields.\n"
    "- Include 'extraction_confidence' (0-1) if that field exists in the schema."
)


def _build_extract_prompt(instruction, masked_kv, schema):
    instruction = (instruction or DEFAULT_INSURANCE_PROMPT).strip()
    return (f"{instruction}\n\nTarget JSON schema:\n{schema}\n\n"
            f"Masked key/values from the document:\n{json.dumps(masked_kv, indent=2)}\n\n"
            "Return only the populated JSON object, preserving any [PII_n] tokens verbatim.")


def _http_post_json(url, headers, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:600]
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url.split('?')[0]}: {e}")


def _call_external_llm(llm, prompt):
    """Call a caller-supplied external LLM (no AWS Bedrock) with a ready prompt.
    Server-side, so no browser CORS issues. Supports Google Gemini, Anthropic,
    and any OpenAI-compatible endpoint. Returns (result_json, provider)."""
    provider = (llm.get("provider") or "").lower().strip()
    api_key  = (llm.get("api_key") or "").strip()
    model    = (llm.get("model") or "").strip()
    base_url = (llm.get("base_url") or "").strip()
    if not api_key:
        raise ValueError("Missing LLM api_key")
    if not model:
        raise ValueError("Missing LLM model")

    if provider == "google":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0, "maxOutputTokens": 4096,
                                        "responseMimeType": "application/json"}}
        data = _http_post_json(url, headers, payload)
        text = data["candidates"][0]["content"]["parts"][0]["text"]

    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"content-type": "application/json", "x-api-key": api_key,
                   "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": 4096, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]}
        data = _http_post_json(url, headers, payload)
        text = data["content"][0]["text"]

    elif provider in ("openai", "openai-compatible", "generic"):
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        url = base + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}
        payload = {"model": model, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]}
        data = _http_post_json(url, headers, payload)
        text = data["choices"][0]["message"]["content"]

    else:
        raise ValueError(f"Unknown provider '{provider}' (use google | anthropic | openai)")

    return _parse_json(text), provider


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
