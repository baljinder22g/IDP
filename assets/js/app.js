/* ===================================================================
   IDP Studio — UI controller
   =================================================================== */
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const API = window.IDP_API;

  // Holds the currently selected file (base64) per tab.
  const files = { bedrock: null, textract: null, agents: null, external: null, azure: null, direct: null };

  // ---------------- theme ----------------
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    $("#themeToggle").textContent = t === "dark" ? "🌙" : "☀️";
    localStorage.setItem("idp.theme", t);
    if (window.mermaid) {
      mermaid.initialize({ startOnLoad: false, theme: t === "dark" ? "dark" : "default" });
      renderDocsDiagrams();
    }
  }
  $("#themeToggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "dark" ? "light" : "dark");
  });

  // ---------------- tabs ----------------
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.setAttribute("aria-selected", "false"));
      tab.setAttribute("aria-selected", "true");
      $$(".panel").forEach((p) => p.classList.remove("active"));
      $("#panel-" + tab.dataset.tab).classList.add("active");
      if (tab.dataset.tab === "logs") refreshLogs();
      if (tab.dataset.tab === "docs") loadDocs();
      if (tab.dataset.tab === "dashboard") refreshDashboard();
    });
  });

  // ---------------- env pill ----------------
  function refreshEnvPill() {
    const pill = $("#envPill"), label = $("#envLabel");
    if (API.isMock()) { pill.classList.remove("live"); label.textContent = "Mock mode"; }
    else { pill.classList.add("live"); label.textContent = "Live: " + API.settings.apiBase.replace(/^https?:\/\//, "").slice(0, 28); }
  }

  // ---------------- settings modal ----------------
  const modal = $("#settingsModal");
  function openSettings() {
    $("#set-apiBase").value = API.settings.apiBase || "";
    $("#set-apiKey").value = API.settings.apiKey || "";
    $("#set-bedrockKey").value = API.settings.bedrockKey || "";
    $("#set-cloud").value = API.settings.cloud || "aws";
    modal.hidden = false;
  }
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#settingsClose").addEventListener("click", () => (modal.hidden = true));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
  $("#settingsSave").addEventListener("click", () => {
    API.saveSettings({
      apiBase: $("#set-apiBase").value.trim(),
      apiKey: $("#set-apiKey").value.trim(),
      bedrockKey: $("#set-bedrockKey").value.trim(),
      cloud: $("#set-cloud").value,
    });
    modal.hidden = true; refreshEnvPill();
  });
  $("#settingsReset").addEventListener("click", () => {
    API.saveSettings({ apiBase: "", apiKey: "" });
    modal.hidden = true; refreshEnvPill();
  });

  // ---------------- file upload wiring ----------------
  function bytesToBase64(buf) {
    let bin = ""; const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
  function humanSize(n) { return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(2) + " MB"; }

  async function handleFile(tabId, file) {
    if (!file) return;
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      alert("Please upload a PDF document."); return;
    }
    const buf = await file.arrayBuffer();
    files[tabId] = { name: file.name, size: file.size, b64: bytesToBase64(buf) };
    const meta = $("#" + tabId + "-file");
    if (meta) {
      meta.hidden = false;
      meta.innerHTML = `<span>📎 <strong>${file.name}</strong> · ${humanSize(file.size)}</span>
        <button class="btn-ghost small" data-clear-file="${tabId}">remove</button>`;
    }
  }

  $$(".dropzone").forEach((dz) => {
    const tabId = dz.dataset.input;
    const input = dz.querySelector("input[type=file]");
    dz.addEventListener("click", () => input.click());
    input.addEventListener("change", (e) => handleFile(tabId, e.target.files[0]));
    ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => handleFile(tabId, e.dataTransfer.files[0]));
  });

  document.addEventListener("click", (e) => {
    const clr = e.target.closest("[data-clear-file]");
    if (clr) {
      const id = clr.dataset.clearFile; files[id] = null;
      const meta = $("#" + id + "-file"); if (meta) { meta.hidden = true; meta.innerHTML = ""; }
    }
  });

  // ---------------- toolbar buttons (samples / format / copy / download) ----------------
  document.addEventListener("click", (e) => {
    const sample = e.target.closest("[data-load-sample]");
    if (sample) { $("#" + sample.dataset.loadSample).value = IDP_CONFIG.sampleSchemaStr; }

    const fmt = e.target.closest("[data-format-json]");
    if (fmt) {
      const el = $("#" + fmt.dataset.formatJson);
      try { el.value = JSON.stringify(JSON.parse(el.value), null, 2); } catch { alert("Not valid JSON yet."); }
    }

    const copy = e.target.closest("[data-copy]");
    if (copy) {
      const el = $("#" + copy.dataset.copy);
      navigator.clipboard.writeText(el.textContent || el.value || "").then(() => flash(copy, "Copied!"));
    }

    const dl = e.target.closest("[data-download]");
    if (dl) {
      const el = $("#" + dl.dataset.download);
      const blob = new Blob([el.textContent || el.value || ""], { type: "application/json" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
      a.download = dl.dataset.download + ".json"; a.click();
    }
  });
  function flash(btn, txt) { const o = btn.textContent; btn.textContent = txt; setTimeout(() => (btn.textContent = o), 1200); }

  function setStatus(id, state) {
    const el = $("#" + id); if (!el) return;
    el.className = "status-chip " + (state === "running" ? "running" : state === "succeeded" || state === "done" ? "done" : state === "error" ? "error" : "");
    el.textContent = state;
  }
  function show(id, obj) { $("#" + id).textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2); }

  function requireInputs(tabId, schemaId, requireSchema = true) {
    if (!files[tabId]) { alert("Upload a PDF first."); return null; }
    const out = {
      filename: files[tabId].name,
      size_kb: Math.round(files[tabId].size / 1024),
      document_base64: files[tabId].b64,
    };
    if (schemaId) {
      const schemaStr = $("#" + schemaId).value.trim();
      if (requireSchema && !schemaStr) { alert("Provide a Target JSON schema."); return null; }
      if (schemaStr) out.target_schema = schemaStr;
    }
    return out;
  }

  // ---------------- TAB 1: Bedrock ----------------
  $("#bedrock-run").addEventListener("click", async () => {
    const base = requireInputs("bedrock", "bedrock-schema"); if (!base) return;
    const req = Object.assign(base, { model: $("#bedrock-model").value, mask_pii: $("#bedrock-mask").checked });
    setStatus("bedrock-status", "running"); show("bedrock-output", "// Calling AWS Bedrock…");
    $("#bedrock-run").disabled = true;
    try {
      const r = await API.extractBedrock(req);
      setStatus("bedrock-status", "succeeded"); show("bedrock-output", r.result);
    } catch (err) { setStatus("bedrock-status", "error"); show("bedrock-output", "Error: " + err.message); }
    finally { $("#bedrock-run").disabled = false; }
  });
  $("#bedrock-clear").addEventListener("click", () => { files.bedrock = null; $("#bedrock-file").hidden = true; show("bedrock-output", "// Result will appear here"); setStatus("bedrock-status", "idle"); });

  // ---------------- TAB 2: Textract ----------------
  $("#textract-run").addEventListener("click", async () => {
    const base = requireInputs("textract", "textract-schema", false); if (!base) return;  // schema optional
    const feats = $$("#textract-features option:checked").map((o) => o.value);
    const req = Object.assign(base, { feature_types: feats, mask_pii: $("#textract-mask").checked });
    setStatus("textract-status", "running"); show("textract-raw", "// Running Textract…"); show("textract-output", "// …");
    $("#textract-run").disabled = true;
    try {
      const r = await API.extractTextract(req);
      const res = r.result || {};
      show("textract-raw", r.raw_keyvalues || {});
      // If a schema was provided, lead with the best-effort mapped JSON.
      show("textract-output", res.target_json
        ? { target_json: res.target_json, mapping: res.mapping, tables: res.tables }
        : res);
      setStatus("textract-status", "succeeded");
    } catch (err) { setStatus("textract-status", "error"); show("textract-output", "Error: " + err.message); }
    finally { $("#textract-run").disabled = false; }
  });
  $("#textract-clear").addEventListener("click", () => { files.textract = null; $("#textract-file").hidden = true; show("textract-raw", "// Key/value pairs Textract found"); show("textract-output", "// Textract result as JSON"); setStatus("textract-status", "idle"); });

  // ---------------- TAB 3: Agents ----------------
  const AGENT_DEFS = [
    { id: "ingest", title: "Ingest Agent", icon: "📥", svc: "S3 / pre-flight" },
    { id: "ocr", title: "OCR Agent", icon: "🔎", svc: "AWS Textract" },
    { id: "pii", title: "PII/PHI Masking Agent", icon: "🛡️", svc: "AWS Comprehend + Medical" },
    { id: "extract", title: "Extraction Agent", icon: "🧠", svc: "AWS Bedrock" },
    { id: "validate", title: "Validation Agent", icon: "✅", svc: "Schema validator" },
  ];
  const EXTERNAL_DEFS = [
    { id: "ingest", title: "Ingest Agent", icon: "📥", svc: "S3 / pre-flight" },
    { id: "ocr", title: "OCR Agent", icon: "🔎", svc: "AWS Textract" },
    { id: "pii", title: "PII/PHI Masking Agent", icon: "🛡️", svc: "AWS Comprehend + Medical" },
    { id: "extract", title: "Extraction Agent", icon: "🧠", svc: "Your LLM" },
    { id: "unmask", title: "Unmasking Agent", icon: "🔓", svc: "Restore original PII/PHI" },
    { id: "finalize", title: "Finalize Agent", icon: "📦", svc: "Validate + deliver" },
  ];
  // Build agent boxes into `flowSel`, id-namespaced by `prefix` so two pipelines coexist.
  function buildAgentBoxes(flowSel, defs, prefix) {
    flowSel = flowSel || "#agents-flow"; defs = defs || AGENT_DEFS; prefix = prefix || "";
    const flow = $(flowSel); flow.innerHTML = "";
    defs.forEach((a, i) => {
      const box = document.createElement("div");
      box.className = "agent-box"; box.id = prefix + "agent-" + a.id;
      box.innerHTML = `
        <div class="ag-state" id="${prefix}agstate-${a.id}"></div>
        <div class="ag-head"><span class="ag-icon">${a.icon}</span>
          <div><div class="ag-title">${i + 1}. ${a.title}</div><div class="ag-svc">${a.svc}</div></div>
        </div>
        <div class="ag-io">
          <label>Input</label><pre id="${prefix}agin-${a.id}">—</pre>
          <label>Output</label><pre id="${prefix}agout-${a.id}">—</pre>
        </div>`;
      flow.appendChild(box);
    });
  }
  // Returns an onStep(id,state,step) callback bound to a prefix.
  function makeUpdater(prefix) {
    prefix = prefix || "";
    return function (id, state, step) {
      const box = $("#" + prefix + "agent-" + id), st = $("#" + prefix + "agstate-" + id);
      if (!box) return;
      box.classList.remove("active", "done", "error");
      const ain = $("#" + prefix + "agin-" + id), aout = $("#" + prefix + "agout-" + id);
      if (state === "idle") { st.textContent = ""; if (ain) ain.textContent = "—"; if (aout) aout.textContent = "—"; return; }
      if (state === "running") { box.classList.add("active"); st.innerHTML = '<span class="spinner"></span>'; }
      else if (state === "done") {
        box.classList.add("done"); st.textContent = "✓";
        if (step) { ain.textContent = JSON.stringify(step.input, null, 1); aout.textContent = JSON.stringify(step.output, null, 1); }
      } else if (state === "error") {
        box.classList.add("error"); st.textContent = "✕";
        if (step) { ain.textContent = JSON.stringify(step.input, null, 1); aout.textContent = step.error ? ("⚠ " + step.error) : JSON.stringify(step.output, null, 1); }
      }
    };
  }
  const updateAgent = makeUpdater("");
  $("#agents-run").addEventListener("click", async () => {
    const base = requireInputs("agents", "agents-schema"); if (!base) return;
    base.detect_phi = $("#agents-phi").checked;   // PHI via Comprehend Medical (extra cost)
    buildAgentBoxes();
    setStatus("agents-status", "running"); show("agents-output", "// Pipeline running…");
    $("#agents-run").disabled = true;
    try {
      const r = await API.runAgents(base, updateAgent);
      if (r.status === "succeeded") {
        setStatus("agents-status", "succeeded"); show("agents-output", r.result);
      } else {
        setStatus("agents-status", "error");
        show("agents-output", {
          status: r.status,
          note: "Pipeline completed steps 1–3, then failed at step 4 (AWS Bedrock).",
          steps_completed: (r.steps || []).filter((s) => s.status !== "error").map((s) => s.id),
          failed_step: r.failed_step || "extract",
          error: r.error, message: r.message,
        });
      }
    } catch (err) { setStatus("agents-status", "error"); show("agents-output", "Error: " + err.message); }
    finally { $("#agents-run").disabled = false; }
  });
  $("#agents-clear").addEventListener("click", () => { files.agents = null; buildAgentBoxes(); show("agents-output", "// Final result after all agents complete"); setStatus("agents-status", "idle"); });

  // ---------------- TAB 4: Agentic + Your LLM ----------------
  const EXT_CREDS_KEY = "idp.external-llm";
  const EXT_HINTS = {
    google:    "Gemini. Model e.g. <b>gemini-2.5-flash</b> (cheapest) or gemini-3.5-flash. Key: Google AI Studio → API keys (aistudio.google.com/apikey).",
    anthropic: "Claude. Model e.g. <b>claude-haiku-4-5</b> (cheapest) or claude-sonnet-4-5. Key: console.anthropic.com → API keys.",
    openai:    "Any OpenAI-compatible API. Set Base URL (e.g. https://api.openai.com/v1, or Groq/OpenRouter). Model e.g. <b>gpt-4o-mini</b>. Key: your provider's dashboard.",
  };
  const EXT_DEFAULT_MODEL = { google: "gemini-2.5-flash", anthropic: "claude-haiku-4-5", openai: "gpt-4o-mini" };

  function extApplyProvider() {
    const p = $("#external-provider").value;
    $("#external-hint").innerHTML = EXT_HINTS[p] || "";
    $("#external-baseurl-field").hidden = (p !== "openai");
    if (!$("#external-model").value) $("#external-model").value = EXT_DEFAULT_MODEL[p] || "";
  }
  function extLoadCreds() {
    try { return JSON.parse(localStorage.getItem(EXT_CREDS_KEY) || "{}"); } catch { return {}; }
  }
  function extApplyCreds(c) {
    if (c.provider) $("#external-provider").value = c.provider;
    if (c.model) $("#external-model").value = c.model;
    if (c.base_url) $("#external-baseurl").value = c.base_url;
    if (c.api_key) $("#external-key").value = c.api_key;
    extApplyProvider();
  }
  $("#external-provider").addEventListener("change", () => { $("#external-model").value = ""; extApplyProvider(); });
  extApplyCreds(extLoadCreds());

  $("#external-creds-save").addEventListener("click", () => {
    const c = { provider: $("#external-provider").value, model: $("#external-model").value.trim(),
                base_url: $("#external-baseurl").value.trim(), api_key: $("#external-key").value.trim() };
    localStorage.setItem(EXT_CREDS_KEY, JSON.stringify(c));
    const st = $("#external-creds-status"); st.textContent = "Saved in this browser."; setTimeout(() => (st.textContent = ""), 2000);
  });
  $("#external-creds-clear").addEventListener("click", () => {
    localStorage.removeItem(EXT_CREDS_KEY); $("#external-key").value = "";
    const st = $("#external-creds-status"); st.textContent = "Cleared."; setTimeout(() => (st.textContent = ""), 2000);
  });

  const updateExternalAgent = makeUpdater("ext-");
  let externalRunId = null;   // set after Stage 1 (prepare)

  // prompt default + reset
  function extSetDefaultPrompt(force) {
    const el = $("#external-prompt");
    if (el && (force || !el.value)) el.value = IDP_CONFIG.defaultPrompt;
  }
  $("#external-prompt-reset").addEventListener("click", () => extSetDefaultPrompt(true));

  // ── Stage 1: prepare (steps 1-3) ──
  $("#external-prepare").addEventListener("click", async () => {
    const base = requireInputs("external", "external-schema", false); if (!base) return;
    base.detect_phi = $("#external-phi").checked;
    buildAgentBoxes("#external-flow", EXTERNAL_DEFS, "ext-");
    setStatus("external-prep-status", "running");
    show("external-ocr", "// running OCR…"); show("external-masked", "// masking…");
    $("#external-prepare").disabled = true; externalRunId = null;
    $("#external-run").disabled = true; $("#external-run-hint").textContent = "Running Stage 1…";
    try {
      const r = await API.agentsPrepare(base, updateExternalAgent);
      externalRunId = r.run_id;
      show("external-ocr", r.ocr_keyvalues || {});
      show("external-masked", r.masked_keyvalues || {});
      setStatus("external-prep-status", "succeeded");
      $("#external-run").disabled = false;
      $("#external-run-hint").textContent = "Stage 1 done (run_id " + r.run_id + "). Now run Stage 2.";
    } catch (err) {
      setStatus("external-prep-status", "error"); show("external-masked", "Error: " + err.message);
      $("#external-run-hint").textContent = "Stage 1 failed.";
    } finally { $("#external-prepare").disabled = false; }
  });

  // ── Stage 2: extract + unmask (steps 4-6) ──
  $("#external-run").addEventListener("click", async () => {
    if (!externalRunId) { alert("Run Stage 1 (OCR + Masking) first."); return; }
    const api_key = $("#external-key").value.trim();
    const model = $("#external-model").value.trim();
    if (!api_key) { alert("Enter your LLM API key."); return; }
    if (!model) { alert("Enter a model id."); return; }
    const req = {
      run_id: externalRunId,
      target_schema: $("#external-schema").value.trim(),
      prompt: $("#external-prompt").value.trim(),
      llm: { provider: $("#external-provider").value, api_key, model, base_url: $("#external-baseurl").value.trim() },
    };
    // reset just the stage-2 boxes
    ["extract", "unmask", "finalize"].forEach((id) => updateExternalAgent(id, "idle"));
    setStatus("external-status", "running");
    show("external-masked-result", "// LLM running…"); show("external-output", "// Calling your LLM, then unmasking…");
    $("#external-run").disabled = true;
    try {
      const r = await API.agentsExtract(req, updateExternalAgent);
      show("external-masked-result", r.masked_result || "// (no masked result returned)");
      if (r.status === "succeeded") {
        setStatus("external-status", "succeeded"); show("external-output", r.result);
      } else {
        setStatus("external-status", "error");
        show("external-output", { status: r.status, note: "Your LLM call (step 4) failed — check provider/model/key.",
          failed_step: r.failed_step || "extract", error: r.error, message: r.message });
      }
    } catch (err) { setStatus("external-status", "error"); show("external-output", "Error: " + err.message); }
    finally { $("#external-run").disabled = false; }
  });

  $("#external-clear").addEventListener("click", () => {
    files.external = null; externalRunId = null;
    const m = $("#external-file"); if (m) m.hidden = true;
    buildAgentBoxes("#external-flow", EXTERNAL_DEFS, "ext-");
    show("external-ocr", "// run Stage 1 to see OCR JSON"); show("external-masked", "// masked JSON appears here");
    show("external-masked-result", "// masked LLM result appears here (still has [PII_n] tokens)");
    show("external-output", "// Final result after Stage 2");
    setStatus("external-prep-status", "idle"); setStatus("external-status", "idle");
    $("#external-run").disabled = true; $("#external-run-hint").textContent = "Run Stage 1 first.";
  });

  // ---------------- TAB 4: Azure ----------------
  $("#azure-run").addEventListener("click", async () => {
    const base = requireInputs("azure", "azure-schema"); if (!base) return;
    const req = Object.assign(base, { model: $("#azure-model").value, deployment: $("#azure-deployment").value, mask_pii: $("#azure-mask").checked });
    setStatus("azure-status", "running"); show("azure-output", "// Calling Azure Document Intelligence…");
    $("#azure-run").disabled = true;
    try {
      const r = await API.extractAzure(req);
      setStatus("azure-status", "succeeded"); show("azure-output", r.result);
    } catch (err) { setStatus("azure-status", "error"); show("azure-output", "Error: " + err.message); }
    finally { $("#azure-run").disabled = false; }
  });
  $("#azure-clear").addEventListener("click", () => { files.azure = null; $("#azure-file").hidden = true; show("azure-output", "// Result will appear here"); setStatus("azure-status", "idle"); });

  // ---------------- TAB 5: Logs ----------------
  let logCache = [];
  async function refreshLogs() {
    const body = $("#logs-body");
    body.innerHTML = `<tr><td colspan="7" class="muted center">Loading…</td></tr>`;
    const { items, warning } = await API.getLogs();
    logCache = items || [];
    renderLogs();
    if (warning) console.warn("logs:", warning);
  }
  function renderLogs() {
    const q = ($("#logs-search").value || "").toLowerCase();
    const f = $("#logs-filter").value;
    const body = $("#logs-body");
    const rows = logCache.filter((l) =>
      (!f || l.capability === f) &&
      (!q || JSON.stringify(l).toLowerCase().includes(q))
    );
    if (!rows.length) { body.innerHTML = `<tr><td colspan="7" class="muted center">No matching logs.</td></tr>`; return; }
    body.innerHTML = rows.map((l, i) => `
      <tr data-log="${i}">
        <td>${new Date(l.timestamp).toLocaleString()}</td>
        <td>${l.run_id || "—"}</td>
        <td>${l.capability}</td>
        <td>${l.model || "—"}</td>
        <td><span class="status-chip ${l.status === "succeeded" ? "done" : l.status === "error" ? "error" : ""}">${l.status || "—"}</span></td>
        <td>${l.latency_ms ? l.latency_ms + " ms" : "—"}</td>
        <td><code>${l.s3_key || "—"}</code></td>
      </tr>`).join("");
    $$("#logs-body tr[data-log]").forEach((tr) => tr.addEventListener("click", () => {
      const idx = +tr.dataset.log;
      show("logs-detail", rows[idx].detail || rows[idx]);
    }));
  }
  $("#logs-refresh").addEventListener("click", refreshLogs);
  $("#logs-search").addEventListener("input", renderLogs);
  $("#logs-filter").addEventListener("change", renderLogs);

  // ---------------- TAB: PII Dashboard ----------------
  const SOURCE_LABELS = {
    comprehend_pii: "Amazon Comprehend (PII)",
    comprehend_phi: "Comprehend Medical (PHI)",
    key_heuristic: "Field-label heuristic",
    regex: "Regex (email/phone/id)",
  };
  let dashItems = [];
  function bars(containerId, obj, labelMap) {
    const el = $("#" + containerId);
    const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
    if (!entries.length) { el.innerHTML = `<div class="muted center small">No data yet.</div>`; return; }
    const max = Math.max(...entries.map((e) => e[1]), 1);
    el.innerHTML = entries.map(([k, v]) => `
      <div class="bar-row">
        <div class="bar-top"><span>${(labelMap && labelMap[k]) || k}</span><span class="bv">${v}</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.round(100 * v / max)}%"></div></div>
      </div>`).join("");
  }
  function statCard(num, lbl, sub) {
    return `<div class="stat-card"><div class="num">${num}</div><div class="lbl">${lbl}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
  }
  async function refreshDashboard() {
    $("#dash-meta").textContent = "Loading…";
    const { aggregate: a, items, warning } = await API.getStats();
    dashItems = items || [];
    const agg = a || { documents: 0 };
    $("#dash-cards").innerHTML = [
      statCard(agg.documents || 0, "Documents processed"),
      statCard((agg.fields_masked || 0) + " / " + (agg.fields_total || 0), "Fields masked / total", (agg.coverage_pct || 0) + "% field coverage"),
      statCard((agg.coverage_pct || 0) + "%", "Field masking coverage"),
      statCard((agg.char_coverage_pct || 0) + "%", "Character coverage", (agg.chars_masked || 0) + " / " + (agg.chars_total || 0) + " chars"),
      statCard(agg.pii_entities || 0, "PII entities (Comprehend)"),
      statCard(agg.phi_entities || 0, "PHI entities (Comprehend Medical)"),
      statCard(agg.entities_masked || 0, "Total masked spans"),
      statCard((agg.fields_unmasked != null ? agg.fields_unmasked : (agg.fields_total || 0) - (agg.fields_masked || 0)), "Fields left unmasked"),
    ].join("");
    bars("dash-source-bars", agg.by_source, SOURCE_LABELS);
    bars("dash-type-bars", agg.by_type, null);
    renderDashTable();
    $("#dash-meta").textContent = (warning ? "⚠ " + warning + " · " : "") +
      `${dashItems.length} document(s) · folder: s3://<logs-bucket>/pii-stats/`;
  }
  function renderDashTable() {
    const body = $("#dash-body");
    if (!dashItems.length) { body.innerHTML = `<tr><td colspan="9" class="muted center">No stats yet — run docs in Tab ③ / ④.</td></tr>`; return; }
    body.innerHTML = dashItems.map((d, i) => {
      const s = d.by_source || {};
      return `<tr data-d="${i}">
        <td>${d.timestamp ? new Date(d.timestamp).toLocaleString() : "—"}</td>
        <td>${d.filename || "—"}</td>
        <td>${d.capability || "—"}</td>
        <td>${d.fields_total || 0}</td>
        <td>${d.fields_masked || 0}</td>
        <td><span class="status-chip done">${d.coverage_pct || 0}%</span></td>
        <td>${d.pii_entities || 0}</td>
        <td>${d.phi_entities || 0}</td>
        <td>${(s.comprehend_pii || 0) + (s.comprehend_phi || 0)}/${s.key_heuristic || 0}/${s.regex || 0}</td>
      </tr>`;
    }).join("");
    $$("#dash-body tr[data-d]").forEach((tr) => tr.addEventListener("click", () => {
      show("dash-detail", dashItems[+tr.dataset.d]);
    }));
  }
  $("#dash-refresh").addEventListener("click", refreshDashboard);

  // ---------------- TAB 7: Direct SDK ----------------
  const DIRECT_CREDS_KEY = "idp.direct-creds";

  function loadDirectCreds() {
    try { return JSON.parse(localStorage.getItem(DIRECT_CREDS_KEY) || "{}"); } catch { return {}; }
  }
  function applyDirectCreds(c) {
    $("#direct-keyid").value = c.accessKeyId || "";
    $("#direct-secret").value = c.secretAccessKey || "";
    $("#direct-token").value = c.sessionToken || "";
    const reg = $("#direct-region");
    if (c.region && [...reg.options].some((o) => o.value === c.region)) reg.value = c.region;
  }

  function generateSdkCode(model, region, schemaStr) {
    const schemaPreview = (() => {
      try { return JSON.stringify(JSON.parse(schemaStr), null, 2); } catch { return schemaStr || "{}"; }
    })();
    return `// Install: npm i @aws-sdk/client-bedrock-runtime
// Auth: env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), ~/.aws/credentials, or IAM role
import { BedrockRuntimeClient, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
import * as fs from "fs";

const client = new BedrockRuntimeClient({ region: "${region || "us-east-1"}" });

const pdfBytes = fs.readFileSync("document.pdf");
const targetSchema = ${schemaPreview};

const response = await client.send(new InvokeModelCommand({
  modelId: "${model}",
  contentType: "application/json",
  accept: "application/json",
  body: JSON.stringify({
    anthropic_version: "bedrock-2023-05-31",
    max_tokens: 4096,
    messages: [{
      role: "user",
      content: [
        {
          type: "document",
          source: { type: "base64", media_type: "application/pdf", data: pdfBytes.toString("base64") }
        },
        {
          type: "text",
          text: \`Extract data from the document and return ONLY valid JSON matching this schema:\\n\${JSON.stringify(targetSchema, null, 2)}\`
        }
      ]
    }]
  })
}));

const raw = JSON.parse(new TextDecoder().decode(response.body));
const extracted = JSON.parse(raw.content[0].text);
console.log(JSON.stringify(extracted, null, 2));`;
  }

  applyDirectCreds(loadDirectCreds());

  $("#direct-creds-save").addEventListener("click", () => {
    const c = {
      accessKeyId: $("#direct-keyid").value.trim(),
      secretAccessKey: $("#direct-secret").value.trim(),
      sessionToken: $("#direct-token").value.trim(),
      region: $("#direct-region").value
    };
    localStorage.setItem(DIRECT_CREDS_KEY, JSON.stringify(c));
    const st = $("#direct-creds-status");
    st.textContent = "Saved.";
    setTimeout(() => (st.textContent = ""), 2000);
  });

  $("#direct-creds-clear").addEventListener("click", () => {
    localStorage.removeItem(DIRECT_CREDS_KEY);
    applyDirectCreds({});
    const st = $("#direct-creds-status");
    st.textContent = "Cleared.";
    setTimeout(() => (st.textContent = ""), 2000);
  });

  $("#direct-run").addEventListener("click", async () => {
    if (!files.direct) { alert("Upload a PDF first."); return; }
    const schemaStr = $("#direct-schema").value.trim();
    if (!schemaStr) { alert("Provide a Target JSON schema."); return; }
    const model = $("#direct-model").value;
    const creds = loadDirectCreds();
    const req = {
      filename: files.direct.name,
      size_kb: Math.round(files.direct.size / 1024),
      document_base64: files.direct.b64,
      target_schema: schemaStr,
      model
    };
    setStatus("direct-status", "running");
    show("direct-output", creds.accessKeyId ? "// Calling AWS Bedrock directly via SDK…" : "// No credentials saved — running mock…");
    $("#direct-run").disabled = true;
    try {
      const r = await API.extractBedrockDirect(req, creds);
      setStatus("direct-status", "succeeded");
      show("direct-output", r.result);
      show("direct-code", generateSdkCode(model, creds.region || "us-east-1", schemaStr));
    } catch (err) {
      setStatus("direct-status", "error");
      show("direct-output", "Error: " + err.message);
      show("direct-code", generateSdkCode(model, creds.region || "us-east-1", schemaStr));
    } finally {
      $("#direct-run").disabled = false;
    }
  });

  $("#direct-clear").addEventListener("click", () => {
    files.direct = null;
    const fm = $("#direct-file"); if (fm) { fm.hidden = true; fm.innerHTML = ""; }
    show("direct-output", "// Result will appear here");
    show("direct-code", "// Run the extraction first to generate the code snippet.");
    setStatus("direct-status", "idle");
  });

  // ---------------- TAB 6: Docs ----------------
  let docsLoaded = false;
  async function loadDocs() {
    if (docsLoaded) return;
    const el = $("#docs-content");
    try {
      const res = await fetch("docs/documentation.md");
      if (!res.ok) throw new Error("not found");
      const md = await res.text();
      renderMarkdownWithMermaid(el, md);
    } catch {
      el.innerHTML = `<p class="muted">Could not load <code>docs/documentation.md</code>. When served via GitHub Pages / a local web server it will render here. You can also open the file directly in the repo.</p>`;
    }
    docsLoaded = true;
  }
  function renderMarkdownWithMermaid(el, md) {
    // 1. Extract mermaid blocks before any markdown parsing.
    const mermaidBlocks = [];
    md = md.replace(/```mermaid\n([\s\S]*?)```/g, (_, code) => {
      mermaidBlocks.push(code.trim());
      return `___MERMAID_${mermaidBlocks.length - 1}___`;
    });
    // 2. Process markdown inside <details> blocks so collapsible sections render properly.
    md = md.replace(/<details>([\s\S]*?)<\/details>/gi, (_, inner) => {
      const parsedInner = window.marked ? marked.parse(inner) : inner;
      return `<details>${parsedInner}</details>`;
    });
    // 3. Parse remaining markdown.
    let html = window.marked ? marked.parse(md) : "<pre>" + md + "</pre>";
    // 4. Inject mermaid diagrams (placeholders may now be inside <p> tags — browsers handle it).
    html = html.replace(/___MERMAID_(\d+)___/g, (_, i) => `<div class="mermaid">${mermaidBlocks[+i]}</div>`);
    el.innerHTML = html;
    renderDocsDiagrams();
  }
  function renderDocsDiagrams() {
    if (!window.mermaid) return;
    $$(".mermaid").forEach((node) => { node.removeAttribute("data-processed"); });
    try { mermaid.run({ nodes: $$(".mermaid") }); } catch (e) { console.warn(e); }
  }

  // ---------------- init ----------------
  function init() {
    applyTheme(localStorage.getItem("idp.theme") || "dark");
    refreshEnvPill();
    $("#buildInfo").textContent = "v" + IDP_CONFIG.build;
    // seed schemas
    ["bedrock-schema", "azure-schema", "agents-schema", "external-schema", "direct-schema"].forEach((id) => {
      const el = $("#" + id); if (el && !el.value) el.value = IDP_CONFIG.sampleSchemaStr;
    });
    buildAgentBoxes();
    buildAgentBoxes("#external-flow", EXTERNAL_DEFS, "ext-");
    extSetDefaultPrompt();
    if (window.mermaid) mermaid.initialize({ startOnLoad: false, theme: "dark" });
  }
  init();
})();
