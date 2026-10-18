#!/usr/bin/env bash
# Deploy: Firebase web config -> Cloud Run -> Pub/Sub push subscription -> Firebase Hosting.
# Run in Cloud Shell from the repo root after `firebase login --no-localhost`:  bash scripts/deploy.sh
set -euo pipefail
PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-asia-south1}
SERVICE=greenroute
TOPIC=greenroute-jobs

command -v firebase >/dev/null || npm install -g firebase-tools

echo ">>> Firebase project + web app config"
firebase projects:addfirebase "$PROJECT_ID" 2>/dev/null || echo "(already a Firebase project)"
if ! firebase apps:list WEB --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "ERROR: Firebase is not enabled on $PROJECT_ID. Open https://console.firebase.google.com,"
  echo "       click 'Add project', pick $PROJECT_ID, accept the terms, then re-run this script."
  exit 1
fi
# Since 15 Oct 2026 Firebase no longer creates the default Hosting site automatically.
# Create it before the web app so the web config gets the right authDomain.
firebase hosting:sites:create "$PROJECT_ID" --project "$PROJECT_ID" --non-interactive 2>/dev/null || echo "(Hosting site exists)"
APP_ID=$(firebase apps:list WEB --project "$PROJECT_ID" --json | python3 -c "import sys,json; r=json.load(sys.stdin).get('result',[]); print(r[0]['appId'] if r else '')")
if [ -z "$APP_ID" ]; then
  firebase apps:create WEB greenroute-web --project "$PROJECT_ID"
  APP_ID=$(firebase apps:list WEB --project "$PROJECT_ID" --json | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['appId'])")
fi
firebase apps:sdkconfig WEB "$APP_ID" --project "$PROJECT_ID" --json | python3 -c "
import sys, json
r = json.load(sys.stdin)['result']
cfg = r.get('sdkConfig') or r
open('public/firebase-config.js', 'w').write('window.FIREBASE_CONFIG = ' + json.dumps(cfg) + ';\n')
print('wrote public/firebase-config.js')"

echo ">>> Cloud Run deploy (build ~3-4 min)"
gcloud run deploy $SERVICE --source . --region "$REGION" --project "$PROJECT_ID" \
  --allow-unauthenticated --min-instances 1 --memory 1Gi --timeout 300 \
  --set-env-vars "PROJECT_ID=$PROJECT_ID,BQ_DATASET=greenroute,PUBSUB_TOPIC=$TOPIC" -q
URL=$(gcloud run services describe $SERVICE --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')
echo "    Cloud Run URL: $URL"

echo ">>> Pub/Sub push subscription -> $URL/api/worker"
gcloud pubsub subscriptions create greenroute-worker --topic $TOPIC --project "$PROJECT_ID" \
  --push-endpoint="$URL/api/worker" --ack-deadline=120 2>/dev/null \
  || gcloud pubsub subscriptions modify-push-config greenroute-worker --project "$PROJECT_ID" --push-endpoint="$URL/api/worker"

echo ">>> Firebase Hosting + Firestore rules"
firebase deploy --only hosting,firestore:rules --project "$PROJECT_ID"

echo ">>> Warm-up (resolves which Gemini models are live per region)"
curl -s "$URL/api/warmup"; echo
SITE=$(firebase hosting:sites:list --project "$PROJECT_ID" --json | python3 -c "
import sys, json
sites = json.load(sys.stdin).get('result', {}).get('sites', [])
d = [s for s in sites if s.get('type') == 'DEFAULT_SITE'] or sites
print(d[0]['name'].split('/')[-1] if d else '')" 2>/dev/null || true)
echo
echo "Prototype:  https://${SITE:-$PROJECT_ID}.web.app"
echo "Cloud Run:  $URL"
