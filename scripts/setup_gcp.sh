#!/usr/bin/env bash
# One-time setup: APIs, Firestore, bucket, Pub/Sub, IAM, BigQuery data + ML models.
# Run in Google Cloud Shell from the repo root:  bash scripts/setup_gcp.sh
set -uo pipefail
PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-asia-south1}
DS=greenroute
BUCKET=gs://${PROJECT_ID}-greenroute
TOPIC=greenroute-jobs
[ -z "$PROJECT_ID" ] && { echo "Set project: gcloud config set project <ID>"; exit 1; }
echo ">>> Project: $PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo ">>> Enabling APIs (1-2 min)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com bigquery.googleapis.com pubsub.googleapis.com firestore.googleapis.com \
  firebase.googleapis.com firebasehosting.googleapis.com storage.googleapis.com compute.googleapis.com \
  --project "$PROJECT_ID"

# Cloud Run and Cloud Build both run as the Compute Engine default service account,
# which only exists once the Compute API is enabled. Wait for it before granting roles.
echo ">>> Waiting for service account $SA"
for i in $(seq 1 30); do
  gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1 && break
  sleep 5
done
gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || { echo "ERROR: $SA not found. Check IAM & Admin > Service Accounts, then re-run."; exit 1; }

echo ">>> Firestore (native) in $REGION"
gcloud firestore databases create --location="$REGION" --type=firestore-native --project "$PROJECT_ID" 2>/dev/null || echo "(Firestore already exists)"

echo ">>> Pub/Sub topic"
gcloud pubsub topics create $TOPIC --project "$PROJECT_ID" 2>/dev/null || echo "(topic exists)"

echo ">>> IAM for Cloud Run service account $SA"
for ROLE in roles/aiplatform.user roles/bigquery.dataViewer roles/bigquery.jobUser roles/datastore.user \
            roles/pubsub.publisher roles/run.builder roles/storage.objectViewer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$ROLE" --condition=None -q >/dev/null && echo "  granted $ROLE" \
    || echo "  ERROR: could not grant $ROLE (see section 8 of the guide)"
done

echo ">>> Bucket + datasets"
gsutil mb -l US "$BUCKET" 2>/dev/null || echo "(bucket exists)"
curl -sL https://raw.githubusercontent.com/GoogleCloudPlatform/region-carbon-info/main/data/yearly/2023.csv | gsutil cp - "$BUCKET/region_carbon_2023.csv"
if ! gsutil -q stat "$BUCKET/azure_llm_conv_1week.csv"; then
  echo "    streaming Azure LLM inference trace (1.1 GB) -> Cloud Storage ..."
  curl -sL https://github.com/Azure/AzurePublicDataset/releases/download/dataset-llm-2024/AzureLLMInferenceTrace_conv_1week.csv \
    | gsutil cp - "$BUCKET/azure_llm_conv_1week.csv"
fi

echo ">>> BigQuery loads"
bq --location=US mk -d --project_id="$PROJECT_ID" $DS 2>/dev/null || echo "(dataset exists)"
bq --project_id="$PROJECT_ID" load --replace --skip_leading_rows=1 --source_format=CSV \
  $DS.region_carbon "$BUCKET/region_carbon_2023.csv" region:STRING,location:STRING,cfe:FLOAT,intensity:FLOAT
bq --project_id="$PROJECT_ID" load --replace --skip_leading_rows=1 --source_format=CSV \
  $DS.trace_raw "$BUCKET/azure_llm_conv_1week.csv" TIMESTAMP:TIMESTAMP,ContextTokens:INTEGER,GeneratedTokens:INTEGER

q() { bq --project_id="$PROJECT_ID" query --use_legacy_sql=false --quiet "$1"; }

echo ">>> Analytics tables"
# Feed the SQL on stdin: passed as an argument, its leading "--" comment is parsed as a bq flag.
sed "s/__DS__/$DS/g" scripts/analytics.sql | bq --project_id="$PROJECT_ID" query --use_legacy_sql=false --quiet

echo ">>> BigQuery ML: output-length model (registered to Vertex AI Model Registry)"
OUTLEN_SELECT="SELECT LN(1+ContextTokens) AS log_ctx, LN(1+GeneratedTokens) AS log_out FROM $DS.trace_raw WHERE RAND() < 0.05"
q "CREATE OR REPLACE MODEL $DS.outlen_model OPTIONS(model_type='LINEAR_REG', input_label_cols=['log_out'], model_registry='VERTEX_AI', vertex_ai_model_id='greenroute_outlen') AS $OUTLEN_SELECT" \
  || q "CREATE OR REPLACE MODEL $DS.outlen_model OPTIONS(model_type='LINEAR_REG', input_label_cols=['log_out']) AS $OUTLEN_SELECT"

echo ">>> BigQuery ML: ARIMA_PLUS demand forecast"
ARIMA_SELECT="SELECT ts, requests FROM $DS.demand_10min"
q "CREATE OR REPLACE MODEL $DS.demand_model OPTIONS(model_type='ARIMA_PLUS', time_series_timestamp_col='ts', time_series_data_col='requests', horizon=144, model_registry='VERTEX_AI', vertex_ai_model_id='greenroute_demand') AS $ARIMA_SELECT" \
  || q "CREATE OR REPLACE MODEL $DS.demand_model OPTIONS(model_type='ARIMA_PLUS', time_series_timestamp_col='ts', time_series_data_col='requests', horizon=144) AS $ARIMA_SELECT"
q "CREATE OR REPLACE TABLE $DS.demand_forecast AS SELECT forecast_timestamp AS ts, forecast_value AS forecast, prediction_interval_lower_bound AS lower, prediction_interval_upper_bound AS upper FROM ML.FORECAST(MODEL $DS.demand_model, STRUCT(144 AS horizon, 0.9 AS confidence_level))"

echo ">>> Done. Check: bq ls $DS"
bq ls --project_id="$PROJECT_ID" $DS
