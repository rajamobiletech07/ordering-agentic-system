#!/usr/bin/env bash
# Deploy one environment to its GKE cluster (used by .github/workflows/_deploy-gke.yml; also runnable
# by hand after `gcloud container clusters get-credentials`).
#
#   deploy/k8s/deploy.sh staging europe-west2-docker.pkg.dev/<project>/ordering/ordering-app@sha256:<digest>
#
# Order of operations (migrations are expand-only, so old pods keep working during the rollout):
#   1. render + server-side dry run of every object
#   2. apply phase=pre objects (namespace, SAs, config, SecretProviderClasses, job templates, base netpols)
#   3. db-migrate job (bootstrap -> alembic -> checkpointer), then db-seed in staging
#   4. apply everything, wait for every Deployment rollout, run the smoke-test job
#      -> on failure, every Deployment is rolled back to its previous ReplicaSet
#   5. rag-ingest job (idempotent; a failure here fails the pipeline without a rollback)
set -euo pipefail

ENV_NAME="${1:?usage: deploy.sh <staging|production> <image@digest>}"
IMAGE="${2:?image reference (prefer @sha256 digest)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=ordering
export NAMESPACE=$NS
export JOB_SUFFIX="${JOB_SUFFIX:-$(date -u +%m%d%H%M%S)}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

echo "::group::render ($ENV_NAME)"
"$HERE/render.sh" "$ENV_NAME" "$IMAGE" > "$OUT/all.yaml"
grep -c '^kind:' "$OUT/all.yaml" | xargs echo "objects:"
echo "::endgroup::"

echo "::group::server-side dry run"
kubectl apply --server-side --field-manager=ordering-deploy --force-conflicts --dry-run=server -f "$OUT/all.yaml" >/dev/null
echo "::endgroup::"

echo "::group::phase pre (config, identities, job templates)"
kubectl apply --server-side --field-manager=ordering-deploy --force-conflicts -l ordering.io/phase=pre -f "$OUT/all.yaml"
echo "::endgroup::"

echo "::group::database migrations"
"$HERE/run-job.sh" db-migrate 900
if kubectl -n $NS get cronjob db-seed >/dev/null 2>&1; then
  "$HERE/run-job.sh" db-seed 900
fi
echo "::endgroup::"

rollback() {
  echo "::error::deploy failed - rolling back Deployments to their previous revision"
  for d in $(kubectl -n $NS get deploy -l app.kubernetes.io/part-of=ordering -o name); do
    kubectl -n $NS rollout undo "$d" || true
  done
  for d in $(kubectl -n $NS get deploy -l app.kubernetes.io/part-of=ordering -o name); do
    kubectl -n $NS rollout status "$d" --timeout=300s || true
  done
}

# Explicit `|| return 1`: errexit is suspended inside functions called from an `if`.
verify_release() {
  echo "::group::rollout"
  for d in $(kubectl -n $NS get deploy -l app.kubernetes.io/part-of=ordering -o name); do
    kubectl -n $NS rollout status "$d" --timeout=600s || return 1
  done
  echo "::endgroup::"
  echo "::group::smoke test"
  "$HERE/run-job.sh" smoke-test 600 || return 1
  echo "::endgroup::"
}

echo "::group::apply all"
kubectl apply --server-side --field-manager=ordering-deploy --force-conflicts -f "$OUT/all.yaml"
echo "::endgroup::"

if ! verify_release; then
  rollback
  exit 1
fi

# After the release is verified: an ingestion failure (e.g. an embedding-provider outage) fails the
# pipeline but does not roll back a healthy app, because retrieval keeps serving current versions.
echo "::group::rag ingestion"
"$HERE/run-job.sh" rag-ingest 1800
echo "::endgroup::"

echo "deployed $IMAGE to $ENV_NAME"
