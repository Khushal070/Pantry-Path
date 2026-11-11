#!/usr/bin/env bash
# Roll back the model server: send all traffic to the previous Cloud Run revision, then
# restore the previous champion record (registry/champion.previous.json, or the second
# newest version in Artifact Registry) and upload it as the newest version.
#   ml/serving/rollback.sh            # previous revision
#   REVISION=<name> ml/serving/rollback.sh
set -euo pipefail
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}" "${GCP_REGION:?set GCP_REGION}"
: "${CLOUD_RUN_SERVICE:?set CLOUD_RUN_SERVICE}"
cd "$(dirname "$0")/.."

current="$(gcloud run services describe "$CLOUD_RUN_SERVICE" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --format 'value(status.latestReadyRevisionName)')"
target="${REVISION:-$(gcloud run revisions list --service "$CLOUD_RUN_SERVICE" \
  --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --sort-by '~metadata.creationTimestamp' \
  --format 'value(metadata.name)' | grep -vx "$current" | head -n 1)}"
if [ -z "$target" ]; then
  echo "No earlier revision of $CLOUD_RUN_SERVICE to roll back to" >&2
  exit 1
fi
echo "Rolling back $CLOUD_RUN_SERVICE: $current -> $target"
gcloud run services update-traffic "$CLOUD_RUN_SERVICE" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --to-revisions "$target=100"

if [ -f registry/champion.previous.json ]; then
  cp registry/champion.json registry/champion.rolled-back.json
  cp registry/champion.previous.json registry/champion.json
elif [ -n "${AR_GENERIC_REPO:-}" ]; then
  previous="$(gcloud artifacts versions list --project "$GCP_PROJECT_ID" \
    --location "$GCP_REGION" --repository "$AR_GENERIC_REPO" --package champion \
    --sort-by '~createTime' --format 'value(name)' | sed -n 2p | sed 's#.*/##')"
  [ -n "$previous" ] || { echo "No previous champion version in Artifact Registry" >&2; exit 1; }
  python -m pantrypath_ml.registry pull-champion --version "$previous"
else
  echo "No previous champion record found; restore registry/champion.json by hand" >&2
  exit 1
fi
if [ -n "${AR_GENERIC_REPO:-}" ]; then
  python -m pantrypath_ml.registry push-champion
fi
echo "Traffic on $target; champion record restored"
