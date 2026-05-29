/* ===================================================================
   IDP Studio — configuration & shared constants
   =================================================================== */
window.IDP_CONFIG = {
  build: "1.0.0",

  // Default settings. Overridden by anything saved in localStorage.
  defaults: {
    apiBase: "",        // empty => mock mode
    apiKey: "",
    cloud: "aws",
  },

  // Single API contract — these paths match api/openapi.yaml and the
  // routes created by the Terraform (API Gateway / Azure APIM).
  endpoints: {
    bedrock:  "/v1/extract/bedrock",
    textract: "/v1/extract/textract",
    agents:   "/v1/agents/run",
    azure:    "/v1/extract/azure",
    logs:     "/v1/logs",
  },

  // A realistic Target JSON for HNW insurance broker underwriting docs.
  sampleSchema: {
    applicant: {
      full_name: "",
      date_of_birth: "",
      national_id: "",            // PII
      email: "",                  // PII
      phone: "",                  // PII
      residential_address: "",    // PII
      nationality: "",
      occupation: ""
    },
    policy: {
      product_type: "",           // e.g. Life / Health / Property
      sum_assured: 0,
      currency: "",
      term_years: 0,
      broker_name: "",
      broker_reference: ""
    },
    medical_questionnaire: {
      height_cm: 0,
      weight_kg: 0,
      smoker: false,
      pre_existing_conditions: [],
      medications: [],
      family_history: []
    },
    financials: {
      annual_income: 0,
      net_worth: 0,
      source_of_wealth: ""
    },
    free_text_notes: "",
    extraction_confidence: 0.0
  }
};

// Convenience: sample schema as a pretty string.
window.IDP_CONFIG.sampleSchemaStr = JSON.stringify(window.IDP_CONFIG.sampleSchema, null, 2);
