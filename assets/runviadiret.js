// Install: npm i @aws-sdk/client-bedrock-runtime
// Auth: env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), ~/.aws/credentials, or IAM role
import { BedrockRuntimeClient, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
import * as fs from "fs";

const client = new BedrockRuntimeClient({ region: "us-east-1" });

const pdfBytes = fs.readFileSync("document.pdf");
const targetSchema = {
  "application": {
    "application_id": "",
    "application_date": "",
    "broker_name": "",
    "policy_type": "",
    "requested_cover_amount": "",
    "currency": ""
  },
  "insured": {
    "full_name": "",
    "date_of_birth": "",
    "age": "",
    "gender": "",
    "residency_country": "",
    "nationality": "",
    "occupation": ""
  },
  "financial_profile": {
    "annual_income": "",
    "net_worth": "",
    "source_of_wealth": "",
    "existing_insurance": ""
  },
  "medical_profile": {
    "height": "",
    "weight": "",
    "smoker_or_tobacco": "",
    "medical_conditions": "",
    "medications": "",
    "surgeries_or_hospitalizations": "",
    "family_medical_history": ""
  },
  "underwriting": {
    "risk_flags": [],
    "missing_information": [],
    "free_text_notes": "",
    "overall_confidence": ""
  }
};

const response = await client.send(new InvokeModelCommand({
  modelId: "anthropic.claude-3-5-sonnet-20241022-v2:0",
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
          text: `Extract data from the document and return ONLY valid JSON matching this schema:\n${JSON.stringify(targetSchema, null, 2)}`
        }
      ]
    }]
  })
}));

const raw = JSON.parse(new TextDecoder().decode(response.body));
const extracted = JSON.parse(raw.content[0].text);
console.log(JSON.stringify(extracted, null, 2));