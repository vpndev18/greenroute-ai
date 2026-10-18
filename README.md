# 🌿 GreenRoute AI: a carbon-aware router for LLM inference

**Track:** Sustainability & Social Impact

AI inference is one of the fastest-growing loads on data centres. Most apps send *every* prompt to the largest
model, in whatever region is closest, the moment it arrives. GreenRoute decides three things per request:

1. **Which model:** Gemini triages prompt complexity and picks the smallest model tier that will answer well.
2. **Where:** it picks the cloud region with the lowest grid carbon intensity, while respecting data-residency rules (an "India only" mode for DPDP).
3. **When:** deferrable batch jobs are queued on Pub/Sub and accounted for in the greenest window before their deadline.

Every request is executed on Gemini (Vertex AI) and logged with its energy (Wh), CO₂ (g) and savings against a
baseline (large model, Mumbai grid, run immediately) in a realtime Firestore ledger.

## How it uses Google Cloud

| Service | Role |
|---|---|
| **Gemini on Vertex AI** | Prompt triage (structured JSON output), answer generation across regions, multimodal image prompts, and a natural-language sustainability analyst |
| **Cloud Run** | FastAPI backend: router, carbon planner, Pub/Sub worker, analytics API |
| **Firebase Hosting** | Web app; `/api/**` is rewritten to Cloud Run |
| **Firestore** | Realtime routing ledger and global CO₂ counters; the dashboard updates live via `onSnapshot` |
| **BigQuery** | Stores Google's region carbon data and 1 week of real Azure production LLM traffic (millions of requests); replays the trace through the routing policy |
| **BigQuery ML → Vertex AI Model Registry** | `outlen_model` (predicts output tokens before execution, for energy estimates) and `demand_model` (ARIMA_PLUS, 24 h demand forecast) |
| **Pub/Sub** | Event-driven queue for deferred batch jobs; a push subscription triggers the Cloud Run worker |
| **Cloud Storage** | Raw dataset landing zone |
| **Gemini vision** | Image attachments are analysed and counted in the energy model |

## Data
- Google Cloud region carbon data (2023): https://github.com/GoogleCloudPlatform/region-carbon-info
- Azure LLM Inference Trace 2024 (DynamoLLM, HPCA 2025, CC-BY): https://github.com/Azure/AzurePublicDataset

## Assumptions (stated honestly)
- Energy per 1k output tokens: Lite 0.25 Wh, Flash 0.6 Wh, Pro 2.5 Wh; input tokens cost 1/10 as much; PUE 1.09. These values are calibrated against Google's published median of 0.24 Wh per Gemini text prompt.
- Hourly grid intensity is *modelled* from annual averages with a daily solar-dip curve. Production would use Electricity Maps or WattTime.
- In demo mode, deferred jobs run when the Pub/Sub message arrives, and their carbon is accounted at the scheduled slot. Production would use Cloud Tasks with an ETA.

## Run it
In Google Cloud Shell:
```bash
gcloud config set project <PROJECT_ID>
bash scripts/setup_gcp.sh      # APIs, Firestore, Pub/Sub, IAM, BigQuery data + ML models (~10 min)
firebase login --no-localhost
bash scripts/deploy.sh         # Cloud Run + Firebase Hosting
```
