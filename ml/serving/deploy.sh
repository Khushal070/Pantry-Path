#!/usr/bin/env bash
# Deploy the model-server image as a new Cloud Run revision (private: callers need an
# identity token). The previous revision stays available for rollback.sh.
#   IMAGE=<region>-docker.pkg.dev/<project>/<repo>/pantrypath-model-server:<tag> \
#     ml/serving/deploy.sh
# Needs: gcloud (authenticated), envsubst, and the variables below.
set -euo pipefail
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}" "${GCP_REGION:?set GCP_REGION}"
: "${CLOUD_RUN_SERVICE:?set CLOUD_RUN_SERVICE}" "${RUN_SERVICE_ACCOUNT:?set RUN_SERVICE_ACCOUNT}"
: "${IMAGE:?set IMAGE (from python -m pantrypath_ml.registry push-image)}"

here="$(cd "$(dirname "$0")" && pwd)"
spec="$(mktemp)"
trap 'rm -f "$spec"' EXIT
export GCP_PROJECT_ID GCP_REGION CLOUD_RUN_SERVICE RUN_SERVICE_ACCOUNT IMAGE
envsubst < "$here/cloudrun.yaml" > "$spec"

gcloud run services replace "$spec" --project "$GCP_PROJECT_ID" --region "$GCP_REGION"
gcloud run services describe "$CLOUD_RUN_SERVICE" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --format 'value(status.url)'
