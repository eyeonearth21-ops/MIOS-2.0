#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-gcp.sh — One-time GCP setup for MIOS 2.0
#
# What this creates:
#   • Artifact Registry repo    (stores the Docker image)
#   • Cloud Run Job             (runs a single MIOS job on demand)
#   • 4 Cloud Scheduler jobs    (fire at IST market times every weekday)
#   • Secret Manager secrets    (stores .env values securely)
#
# Pre-requisites:
#   1. gcloud CLI installed and authenticated  →  gcloud auth login
#   2. Billing enabled on the project
#   3. Run once:  chmod +x deploy-gcp.sh && ./deploy-gcp.sh
#
# Cost:  Cloud Run Jobs + Scheduler stays inside the free tier for 4 jobs/day.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration — edit these ────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:-your-gcp-project-id}"   # gcloud projects list
REGION="${GCP_REGION:-asia-south1}"                    # Mumbai — closest to NSE
AR_REPO="mios"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/mios"
JOB_NAME="mios"
SA_NAME="mios-runner"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "━━━ MIOS GCP Setup ━━━"
echo "  Project : ${PROJECT_ID}"
echo "  Region  : ${REGION}"
echo "  Image   : ${IMAGE}"
echo ""

# ── Enable required APIs ──────────────────────────────────────────────────────
echo "▶ Enabling GCP APIs …"
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --project="${PROJECT_ID}"

# ── Service account ───────────────────────────────────────────────────────────
echo "▶ Creating service account ${SA_EMAIL} …"
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="MIOS Cloud Run runner" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  (already exists — skipping)"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" --quiet

# ── Artifact Registry repo ────────────────────────────────────────────────────
echo "▶ Creating Artifact Registry repo '${AR_REPO}' (skipped if exists) …"
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "  (already exists — skipping)"

echo "▶ Granting Cloud Build SA push access …"
CLOUDBUILD_SA="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')@cloudbuild.gserviceaccount.com"
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/artifactregistry.writer" --quiet

# ── Build & push Docker image ─────────────────────────────────────────────────
echo "▶ Building and pushing Docker image …"
gcloud builds submit --tag "${IMAGE}" --project="${PROJECT_ID}" .

# ── Store secrets in Secret Manager ──────────────────────────────────────────
echo "▶ Storing secrets in Secret Manager …"
store_secret() {
  local name="$1" value="$2"
  echo -n "${value}" | gcloud secrets create "${name}" \
    --data-file=- --project="${PROJECT_ID}" 2>/dev/null \
  || echo -n "${value}" | gcloud secrets versions add "${name}" \
    --data-file=- --project="${PROJECT_ID}"
}

# Load .env values (if file exists locally)
if [[ -f .env ]]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

store_secret "MIOS_EMAIL_ADDRESS"    "${EMAIL_ADDRESS:-}"
store_secret "MIOS_EMAIL_PASSWORD"   "${EMAIL_PASSWORD:-}"
store_secret "MIOS_EMAIL_RECEIVER"   "${EMAIL_RECEIVER:-}"
store_secret "MIOS_SMTP_SERVER"      "${SMTP_SERVER:-smtp.gmail.com}"
store_secret "MIOS_SMTP_PORT"        "${SMTP_PORT:-587}"

echo "  Secrets stored. Non-sensitive config is passed as plain env vars."

# ── Create / update the Cloud Run Job ─────────────────────────────────────────
echo "▶ Creating Cloud Run Job '${JOB_NAME}' …"
gcloud run jobs create "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="EMAIL_ADDRESS=MIOS_EMAIL_ADDRESS:latest,EMAIL_PASSWORD=MIOS_EMAIL_PASSWORD:latest,EMAIL_RECEIVER=MIOS_EMAIL_RECEIVER:latest,SMTP_SERVER=MIOS_SMTP_SERVER:latest,SMTP_PORT=MIOS_SMTP_PORT:latest" \
  --set-env-vars="SCANNER_LOOKBACK_DAYS=20,SCANNER_VOLUME_SPIKE=1.5,SCANNER_MIN_PRICE_CHG=0.5,RISK_SL_ATR_MULT=1.5,RISK_MAX_SL_PCT=0.03,RISK_T2_RR_MULTIPLE=2.0,ALERT_MIN_SCORE=70,LOG_LEVEL=INFO" \
  --task-timeout=600 \
  --max-retries=1 \
  --project="${PROJECT_ID}" 2>/dev/null \
|| gcloud run jobs update "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}"

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

# ── Cloud Scheduler — 4 jobs (IST = UTC+5:30, weekdays only) ─────────────────
# IST → UTC:  08:45→03:15  09:20→03:50  12:30→07:00  15:35→10:05
echo "▶ Setting up Cloud Scheduler jobs (Mon-Fri, IST) …"

create_schedule() {
  local sched_name="$1" cron_utc="$2" run_arg="$3"
  gcloud scheduler jobs create http "${sched_name}" \
    --location="${REGION}" \
    --schedule="${cron_utc}" \
    --time-zone="UTC" \
    --uri="${JOB_URI}" \
    --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}" \
    --message-body="{\"overrides\":{\"containerOverrides\":[{\"args\":[\"--run\",\"${run_arg}\"]}]}}" \
    --project="${PROJECT_ID}" 2>/dev/null \
  || echo "  ${sched_name} already exists — skipping"
}

#                  name                    cron (UTC weekdays)  mios --run arg
create_schedule  "mios-pre-market"        "15 3 * * 1-5"       "pre_market"
create_schedule  "mios-opening-scan"      "50 3 * * 1-5"       "opening_scan"
create_schedule  "mios-midday-scan"       "0 7 * * 1-5"        "midday_scan"
create_schedule  "mios-closing-report"    "5 10 * * 1-5"       "closing_report"

echo ""
echo "━━━ Done! ━━━"
echo "Cloud Run Job  : https://console.cloud.google.com/run/jobs?project=${PROJECT_ID}"
echo "Scheduler jobs : https://console.cloud.google.com/cloudscheduler?project=${PROJECT_ID}"
echo ""
echo "Test a job manually:"
echo "  gcloud run jobs execute ${JOB_NAME} --region=${REGION} --args='--run,pre_market' --project=${PROJECT_ID}"
