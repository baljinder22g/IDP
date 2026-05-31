/* ===================================================================
   IDP Studio — API client
   - When a real API base URL is configured, calls the backend
     (AWS API Gateway or Azure APIM) per the OpenAPI contract.
   - When no base URL is set, falls back to a local MOCK that
     simulates Bedrock / Textract / Azure responses so the SPA is
     fully demoable on GitHub Pages with zero infrastructure.
   =================================================================== */
(function () {
  const LS_KEY = "idp.settings";

  function loadSettings() {
    try {
      return Object.assign({}, IDP_CONFIG.defaults, JSON.parse(localStorage.getItem(LS_KEY) || "{}"));
    } catch { return Object.assign({}, IDP_CONFIG.defaults); }
  }
  function saveSettings(s) { localStorage.setItem(LS_KEY, JSON.stringify(s)); }

  const settings = loadSettings();

  function isMock() { return !settings.apiBase; }

  function headers() {
    const h = { "Content-Type": "application/json" };
    if (settings.apiKey) h["x-api-key"] = settings.apiKey;
    return h;
  }

  // ---------- real HTTP call ----------
  async function http(path, body, method = "POST") {
    const url = settings.apiBase.replace(/\/$/, "") + path;
    const opts = { method, headers: headers() };
    if (body && method !== "GET") opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    const text = await res.text();
    let data; try { data = JSON.parse(text); } catch { data = { raw: text }; }
    if (!res.ok) throw new Error(data.message || data.error || ("HTTP " + res.status));
    return data;
  }

  // ---------- mock helpers ----------
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  function fakeMask(str) {
    return String(str)
      .replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, "[EMAIL]")
      .replace(/\b\d{6,}\b/g, "[ID]")
      .replace(/\+?\d[\d\s-]{7,}\d/g, "[PHONE]");
  }

  // Build a plausible filled-in version of the requested schema.
  function fillSchema(schema, seed) {
    const samp = {
      full_name: "Jonathan A. Whitfield",
      date_of_birth: "1974-03-12",
      national_id: "[ID]",
      email: "[EMAIL]",
      phone: "[PHONE]",
      residential_address: "14 Eaton Square, London SW1W 9DD",
      nationality: "British",
      occupation: "Managing Partner, Private Equity",
      product_type: "Whole-of-Life (HNW)",
      sum_assured: 12000000,
      currency: "GBP",
      term_years: 0,
      broker_name: "Sterling Private Risk Ltd",
      broker_reference: "SPR-2026-00481",
      height_cm: 183, weight_kg: 88, smoker: false,
      pre_existing_conditions: ["Hypertension (controlled)"],
      medications: ["Amlodipine 5mg"],
      family_history: ["Father: cardiac, age 71"],
      annual_income: 2400000, net_worth: 48000000,
      source_of_wealth: "Business ownership and investment portfolio",
      free_text_notes: "Client requests expedited underwriting; frequent international travel.",
      extraction_confidence: 0.93
    };
    function walk(node) {
      if (Array.isArray(node)) return node.length ? node : ["(see source)"];
      if (node && typeof node === "object") {
        const out = {};
        for (const k of Object.keys(node)) {
          out[k] = (k in samp) ? samp[k] : walk(node[k]);
        }
        return out;
      }
      // leaf — leave example value as-is if present, else placeholder
      return node;
    }
    // Prefer to fill known keys; fall back to recursive walk.
    function deep(node) {
      if (Array.isArray(node)) return node.length ? node : ["(none found)"];
      if (node && typeof node === "object") {
        const o = {}; for (const k in node) o[k] = (k in samp && (typeof node[k] !== "object")) ? samp[k] : deep(node[k]); return o;
      }
      return node;
    }
    try { return deep(schema); } catch { return samp; }
  }

  function parseSchema(schemaStr) {
    try { return JSON.parse(schemaStr); } catch { return IDP_CONFIG.sampleSchema; }
  }

  async function mockBedrock(req) {
    await wait(1400);
    const schema = parseSchema(req.target_schema);
    return {
      run_id: "run_" + Date.now().toString(36),
      capability: "bedrock",
      model: req.model,
      status: "succeeded",
      latency_ms: 1380,
      result: fillSchema(schema),
      _mock: true
    };
  }

  async function mockTextract(req) {
    await wait(1100);
    const kv = {
      "Full Name": "Jonathan A. Whitfield",
      "Date of Birth": "12/03/1974",
      "Email": "[EMAIL]",
      "Policy Type": "Whole-of-Life (HNW)",
      "Sum Assured": "GBP 12,000,000",
      "Smoker": "No",
      "Annual Income": "GBP 2,400,000"
    };
    const tables = [
      [["Condition", "Year", "Status"],
       ["Hypertension", "2019", "Controlled"],
       ["None other", "-", "-"]]
    ];
    const result = { source: "AWS Textract (no LLM)", key_values: kv, tables, summary: { kv_pairs: Object.keys(kv).length, tables: tables.length } };
    if (req.target_schema) {
      try {
        result.target_json = fillSchema(JSON.parse(req.target_schema));
        result.mapping = { matched: 6, total: 10, method: "fuzzy key match (no LLM) — mock" };
      } catch { /* invalid schema → skip mapping */ }
    }
    return {
      run_id: "run_" + Date.now().toString(36),
      capability: "textract",
      service: "AWS Textract (forms + tables → JSON, no LLM)",
      status: "succeeded",
      latency_ms: 1040,
      raw_keyvalues: kv,
      result,
      _mock: true
    };
  }

  async function mockBedrockDirect(req) {
    await wait(1100);
    const schema = parseSchema(req.target_schema);
    return {
      run_id: "sdk_" + Date.now().toString(36),
      capability: "bedrock-direct",
      model: req.model,
      status: "succeeded",
      latency_ms: 1060,
      path: "AWS SDK → Bedrock Runtime (no API Gateway / Lambda)",
      result: fillSchema(schema),
      _mock: true
    };
  }

  async function mockAzure(req) {
    await wait(1250);
    const schema = parseSchema(req.target_schema);
    return {
      run_id: "run_" + Date.now().toString(36),
      capability: "azure",
      service: "Azure Document Intelligence (" + req.model + ") + Azure OpenAI",
      status: "succeeded",
      latency_ms: 1190,
      result: fillSchema(schema),
      _mock: true
    };
  }

  // Mock agent pipeline with per-step callback for live UI updates.
  async function mockAgents(req, onStep) {
    const schema = parseSchema(req.target_schema);
    const steps = [
      { id: "ingest",   title: "Ingest Agent",     icon: "📥", svc: "S3 / pre-flight",
        input: { file: req.filename, size_kb: req.size_kb || 0 },
        output: { pages: 4, mime: "application/pdf", stored: "s3://idp-input/" + (req.filename||"doc.pdf") } },
      { id: "ocr",      title: "OCR Agent",        icon: "🔎", svc: "AWS Textract",
        input: { s3_key: "input/" + (req.filename||"doc.pdf") },
        output: { blocks: 312, kv_pairs: 27, tables: 2, confidence: 0.97 } },
      { id: "pii",      title: "PII Masking Agent",icon: "🛡️", svc: "Comprehend / regex",
        input: { kv_pairs: 27 },
        output: { masked_fields: ["email","national_id","phone"], pii_found: 3 } },
      { id: "extract",  title: "Extraction Agent", icon: "🧠", svc: "AWS Bedrock (Claude)",
        input: { target_keys: Object.keys(schema).length, masked: true },
        output: fillSchema(schema) },
      { id: "validate", title: "Validation Agent", icon: "✅", svc: "Schema validator",
        input: { against: "target_schema" },
        output: { valid: true, missing_required: [], confidence: 0.93 } },
    ];
    for (const s of steps) {
      onStep && onStep(s.id, "running", null);
      await wait(900);
      onStep && onStep(s.id, "done", s);
    }
    return {
      run_id: "run_" + Date.now().toString(36),
      capability: "agents",
      status: "succeeded",
      latency_ms: steps.length * 900,
      steps,
      result: steps.find((s) => s.id === "extract").output,
      _mock: true
    };
  }

  // In-memory mock log store (also persisted to localStorage).
  function mockLogStore() {
    try { return JSON.parse(localStorage.getItem("idp.logs") || "[]"); } catch { return []; }
  }
  function pushMockLog(entry) {
    const logs = mockLogStore(); logs.unshift(entry);
    localStorage.setItem("idp.logs", JSON.stringify(logs.slice(0, 200)));
  }

  // ---------- public API ----------
  const API = {
    settings,
    isMock,
    saveSettings(next) { Object.assign(settings, next); saveSettings(settings); },

    recordLog(entry) {
      const e = Object.assign({ timestamp: new Date().toISOString() }, entry);
      pushMockLog(e); // mirror locally even in live mode for instant UX
      return e;
    },

    // Attach the optional Bedrock bearer token to any request that hits Bedrock.
    _withBedrockKey(req) {
      if (settings.bedrockKey) req.bedrock_api_key = settings.bedrockKey;
      return req;
    },

    async extractBedrock(req) {
      req = this._withBedrockKey(req);
      const r = isMock() ? await mockBedrock(req) : await http(IDP_CONFIG.endpoints.bedrock, req);
      this.recordLog({ run_id: r.run_id, capability: "bedrock", model: r.model || req.model, status: r.status, latency_ms: r.latency_ms, s3_key: r.s3_key || ("logs/bedrock/" + r.run_id + ".json"), detail: r });
      return r;
    },
    async extractTextract(req) {
      req = this._withBedrockKey(req);
      const r = isMock() ? await mockTextract(req) : await http(IDP_CONFIG.endpoints.textract, req);
      this.recordLog({ run_id: r.run_id, capability: "textract", model: r.service || "Textract", status: r.status, latency_ms: r.latency_ms, s3_key: r.s3_key || ("logs/textract/" + r.run_id + ".json"), detail: r });
      return r;
    },
    async extractAzure(req) {
      const r = isMock() ? await mockAzure(req) : await http(IDP_CONFIG.endpoints.azure, req);
      this.recordLog({ run_id: r.run_id, capability: "azure", model: r.service || req.model, status: r.status, latency_ms: r.latency_ms, s3_key: r.s3_key || ("logs/azure/" + r.run_id + ".json"), detail: r });
      return r;
    },
    async runAgents(req, onStep) {
      req = this._withBedrockKey(req);
      if (isMock()) {
        const r = await mockAgents(req, onStep);
        this.recordLog({ run_id: r.run_id, capability: "agents", model: "multi-agent", status: r.status, latency_ms: r.latency_ms, s3_key: "logs/agents/" + r.run_id + ".json", detail: r });
        return r;
      }
      // Live mode: backend returns the full step list (no streaming in this contract).
      const r = await http(IDP_CONFIG.endpoints.agents, req);
      (r.steps || []).forEach((s) => onStep && onStep(s.id, s.status === "error" ? "error" : "done", s));
      this.recordLog({ run_id: r.run_id, capability: "agents", model: "multi-agent", status: r.status, latency_ms: r.latency_ms, s3_key: r.s3_key || ("logs/agents/" + r.run_id + ".json"), detail: r });
      return r;
    },
    async extractBedrockDirect(req, creds) {
      const hasCreds = creds && creds.accessKeyId && creds.secretAccessKey;
      if (!hasCreds) {
        const r = await mockBedrockDirect(req);
        this.recordLog({ run_id: r.run_id, capability: "bedrock-direct", model: r.model, status: r.status, latency_ms: r.latency_ms, s3_key: "local/sdk-direct/" + r.run_id + ".json", detail: r });
        return r;
      }
      // Attempt a real SDK call via ESM CDN. Will fail in browser due to CORS.
      try {
        const { BedrockRuntimeClient, InvokeModelCommand } = await import("https://esm.sh/@aws-sdk/client-bedrock-runtime@3");
        const client = new BedrockRuntimeClient({
          region: creds.region || "us-east-1",
          credentials: {
            accessKeyId: creds.accessKeyId,
            secretAccessKey: creds.secretAccessKey,
            ...(creds.sessionToken ? { sessionToken: creds.sessionToken } : {})
          }
        });
        const prompt = "Extract structured data from the attached PDF and return ONLY valid JSON matching this schema:\n" + req.target_schema;
        const body = JSON.stringify({
          anthropic_version: "bedrock-2023-05-31",
          max_tokens: 4096,
          messages: [{ role: "user", content: [
            { type: "document", source: { type: "base64", media_type: "application/pdf", data: req.document_base64 } },
            { type: "text", text: prompt }
          ]}]
        });
        const t0 = Date.now();
        const resp = await client.send(new InvokeModelCommand({
          modelId: req.model,
          contentType: "application/json",
          accept: "application/json",
          body: new TextEncoder().encode(body)
        }));
        const decoded = JSON.parse(new TextDecoder().decode(resp.body));
        const text = decoded.content[0].text;
        let result; try { result = JSON.parse(text.match(/\{[\s\S]*\}/)[0]); } catch { result = { raw: text }; }
        const r = { run_id: "sdk_" + Date.now().toString(36), capability: "bedrock-direct", model: req.model, status: "succeeded", latency_ms: Date.now() - t0, path: "AWS SDK → Bedrock Runtime (direct)", result };
        this.recordLog({ run_id: r.run_id, capability: "bedrock-direct", model: r.model, status: r.status, latency_ms: r.latency_ms, s3_key: "local/sdk-direct/" + r.run_id + ".json", detail: r });
        return r;
      } catch (err) {
        const msg = (err.name === "TypeError" || err.message.toLowerCase().includes("cors") || err.message.toLowerCase().includes("fetch"))
          ? "CORS blocked: AWS Bedrock Runtime does not allow direct browser calls. Use the Node.js code snippet, or route via API Gateway (Tab ①)."
          : err.message;
        throw new Error(msg);
      }
    },

    async getLogs() {
      if (isMock()) return { items: mockLogStore() };
      try {
        const r = await http(IDP_CONFIG.endpoints.logs, null, "GET");
        return r;
      } catch (e) {
        // fall back to locally mirrored logs if the endpoint isn't reachable
        return { items: mockLogStore(), warning: String(e.message) };
      }
    },
  };

  window.IDP_API = API;
})();
