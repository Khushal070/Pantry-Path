#!/usr/bin/env bash
# One-time GCP setup for the model pipeline's CI/CD (.github/workflows/model-pipeline.yml):
# APIs, the two Artifact Registry repositories, a runtime and a CI service account with
# the roles they need, and workload identity federation so GitHub Actions can deploy
# without a key file. Re-running it is safe: existing resources are kept.
#
#   GCP_PROJECT_ID=pantry-path GCP_REGION=us-east1 GITHUB_REPO=Khushal070/Pantry-Path \
#     bash infra/gcp/bootstrap.sh
#
# Needs: gcloud, logged in as a project Owner (or IAM + Artifact Registry + Run admin).
# At the end it prints the GitHub repository variables and secrets to set.
# Review before running: it changes IAM on the project.
set -euo pipefail
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}" "${GCP_REGION:?set GCP_REGION (e.g. us-east1)}"
: "${GITHUB_REPO:?set GITHUB_REPO as owner/name, e.g. Khushal070/Pantry-Path}"
AR_DOCKER_REPO="${AR_DOCKER_REPO:-pantrypath-images}"
AR_GENERIC_REPO="${AR_GENERIC_REPO:-pantrypath-models}"
CLOUD_RUN_SERVICE="${CLOUD_RUN_SERVICE:-pantrypath-model-server}"
POOL="${WIF_POOL:-github}"
PROVIDER="${WIF_PROVIDER:-github-actions}"
RUNTIME_SA="pp-model-runtime@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
CI_SA="pp-model-ci@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
P=(--project "$GCP_PROJECT_ID")
quiet() { "$@" >/dev/null 2>&1; }

echo "== APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com "${P[@]}"

echo "== Artifact Registry: $AR_DOCKER_REPO (docker), $AR_GENERIC_REPO (generic)"
quiet gcloud artifacts repositories describe "$AR_DOCKER_REPO" --location "$GCP_REGION" "${P[@]}" \
  || gcloud artifacts repositories create "$AR_DOCKER_REPO" --repository-format docker \
       --location "$GCP_REGION" --description "Pantry Path model-server images" "${P[@]}"
quiet gcloud artifacts repositories describe "$AR_GENERIC_REPO" --location "$GCP_REGION" "${P[@]}" \
  || gcloud artifacts repositories create "$AR_GENERIC_REPO" --repository-format generic \
       --location "$GCP_REGION" --description "Pantry Path champion records" "${P[@]}"

echo "== Service accounts"
for name in pp-model-runtime pp-model-ci; do
  quiet gcloud iam service-accounts describe "$name@${GCP_PROJECT_ID}.iam.gserviceaccount.com" "${P[@]}" \
    || gcloud iam service-accounts create "$name" --display-name "Pantry Path $name" "${P[@]}"
done

echo "== Roles for the CI account"
for role in roles/artifactregistry.writer roles/run.admin roles/run.invoker; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member "serviceAccount:$CI_SA" \
    --role "$role" --condition None >/dev/null
done
# deploy.sh runs the service as the runtime account
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" "${P[@]}" \
  --member "serviceAccount:$CI_SA" --role roles/iam.serviceAccountUser >/dev/null

echo "== Workload identity federation for $GITHUB_REPO"
number="$(gcloud projects describe "$GCP_PROJECT_ID" --format 'value(projectNumber)')"
quiet gcloud iam workload-identity-pools describe "$POOL" --location global "${P[@]}" \
  || gcloud iam workload-identity-pools create "$POOL" --location global \
       --display-name "GitHub Actions" "${P[@]}"
quiet gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global \
  --workload-identity-pool "$POOL" "${P[@]}" \
  || gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
       --workload-identity-pool "$POOL" --issuer-uri "https://token.actions.githubusercontent.com" \
       --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
       --attribute-condition "assertion.repository == '$GITHUB_REPO'" "${P[@]}"
principal="principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$POOL/attribute.repository/$GITHUB_REPO"
# act as the CI account, and mint ID tokens for the smoke test after a deploy
for role in roles/iam.workloadIdentityUser roles/iam.serviceAccountOpenIdTokenCreator; do
  gcloud iam service-accounts add-iam-policy-binding "$CI_SA" "${P[@]}" \
    --member "$principal" --role "$role" >/dev/null
done

cat <<EOF

Done. In GitHub (Settings -> Secrets and variables -> Actions):

Variables
  GCP_PROJECT_ID       $GCP_PROJECT_ID
  GCP_REGION           $GCP_REGION
  AR_DOCKER_REPO       $AR_DOCKER_REPO
  AR_GENERIC_REPO      $AR_GENERIC_REPO
  CLOUD_RUN_SERVICE    $CLOUD_RUN_SERVICE
  RUN_SERVICE_ACCOUNT  $RUNTIME_SA

Secrets
  GCP_WORKLOAD_IDENTITY_PROVIDER  projects/$number/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER
  GCP_SERVICE_ACCOUNT             $CI_SA
  (optional) ALERT_EMAIL_TO, ALERT_SMTP_USER, ALERT_SMTP_PASSWORD, SLACK_WEBHOOK_URL

The backend's own service account will need roles/run.invoker on $CLOUD_RUN_SERVICE.
Set a billing budget alert in the console (Billing -> Budgets & alerts): a student credit
runs out quietly otherwise.
EOF
