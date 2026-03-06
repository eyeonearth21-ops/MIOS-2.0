#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-webapp-gcp.sh — Deploy MIOS Streamlit web app to Cloud Run
#
# Creates a public HTTPS URL (e.g. https://mios-web-xxxx-el.a.run.app)
# where you can open the scanner in any browser — no install needed.
#
# Cloud Run free tier: 2M requests/month, scales to 0 when idle → $0 cost.
#
# Pre-requisites:
#   gcloud auth login && gcloud auth configure-docker
#   chmod +x deploy-webapp-gcp.sh && ./deploy-webapp-gcp.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-your-gcp-project-id}"
REGION="${GCP_REGION:-asia-south1}"
SERVICE_NAME="mios-web"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "━━━ MIOS Web App — Cloud Run Deploy ━━━"
echo "  Project : ${PROJECT_ID}"
echo "  Region  : ${REGION}"
echo ""

# Enable APIs
gcloud services enable run.googleapis.com secretmanager.googleapis.com \
  --project="${PROJECT_ID}"

# Build image using Cloud Build (no local Docker required)
echo "▶ Building image with Cloud Build …"
gcloud builds submit \
  --tag "${IMAGE}" \
  --project "${PROJECT_ID}" \
  -f Dockerfile.web \
  .

# Store Gmail secret (one-time; skip if already set)
store_secret() {
  local name="$1" value="$2"
  echo -n "${value}" | gcloud secrets create "${name}" \
    --data-file=- --project="${PROJECT_ID}" 2>/dev/null \
  || echo -n "${value}" | gcloud secrets versions add "${name}" \
    --data-file=- --project="${PROJECT_ID}"
}

if [[ -f .env ]]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

echo "▶ Storing Gmail credentials in Secret Manager …"
store_secret "MIOS_EMAIL_ADDRESS"  "${EMAIL_ADDRESS:-}"
store_secret "MIOS_EMAIL_PASSWORD" "${EMAIL_PASSWORD:-}"
store_secret "MIOS_EMAIL_RECEIVER" "${EMAIL_RECEIVER:-}"

# Deploy to Cloud Run (allow unauthenticated = public URL)
echo "▶ Deploying Cloud Run service '${SERVICE_NAME}' …"
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 2 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 120 \
  --set-secrets "EMAIL_ADDRESS=MIOS_EMAIL_ADDRESS:latest,EMAIL_PASSWORD=MIOS_EMAIL_PASSWORD:latest,EMAIL_RECEIVER=MIOS_EMAIL_RECEIVER:latest" \
  --set-env-vars "SCANNER_LOOKBACK_DAYS=20,SCANNER_VOLUME_SPIKE=1.5,SCANNER_MIN_PRICE_CHG=0.5,RISK_SL_ATR_MULT=1.5,RISK_MAX_SL_PCT=0.03,ALERT_MIN_SCORE=70,LOG_LEVEL=INFO" \
  --project "${PROJECT_ID}"

# Print the live URL
URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format "value(status.url)")

echo ""
echo "━━━ Done! ━━━"
echo "🌐 Web app live at: ${URL}"
echo ""
echo "To redeploy after code changes, just run this script again."
