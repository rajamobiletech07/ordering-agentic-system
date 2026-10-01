#!/usr/bin/env bash
# Render the manifests for one environment to stdout.
#
#   deploy/k8s/render.sh staging  europe-west2-docker.pkg.dev/p/ordering/ordering-app@sha256:...
#   deploy/k8s/render.sh production "$IMAGE" > rendered.yaml
#
# `kubectl kustomize` builds the overlay, then envsubst fills in the infrastructure identifiers
# from overlays/<env>/values.env plus IMAGE. Only an explicit allowlist of variables is
# substituted, so shell-style strings in commands (e.g. ${EMBEDDING_PROFILE}) pass through.
set -euo pipefail

ENV_NAME="${1:?usage: render.sh <staging|production> [image]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY="$HERE/overlays/$ENV_NAME"
[[ -d "$OVERLAY" ]] || { echo "unknown environment: $ENV_NAME" >&2; exit 2; }

set -a
# shellcheck disable=SC1091
source "$OVERLAY/values.env"
set +a
export IMAGE="${2:-${IMAGE:-$IMAGE_REPO:unset}}"

# The LiteLLM component carries a copy of the compose config; refuse to deploy a drifted copy.
if ! cmp -s "$HERE/../../docker/litellm/config.yaml" "$HERE/components/litellm/litellm-config.yaml"; then
  echo "components/litellm/litellm-config.yaml differs from docker/litellm/config.yaml - copy it over" >&2
  exit 1
fi

# shellcheck disable=SC2016  # literal ${VAR} names for envsubst's allowlist
VARS='${PROJECT_ID} ${REGION} ${IMAGE} ${APP_HOST} ${STATIC_IP_NAME} ${SECURITY_POLICY} ${SSL_POLICY} ${PRIVATE_SERVICES_CIDR} ${RAG_BUCKET} ${AGENTOPS_BUCKET}'
RENDERED="$(kubectl kustomize "$OVERLAY" | envsubst "$VARS")"

# Fail loudly if any allowlisted placeholder survived (i.e. values.env is missing a key).
for v in $VARS; do
  if grep -qF -- "$v" <<<"$RENDERED"; then echo "unsubstituted $v in $ENV_NAME render" >&2; exit 1; fi
done
printf '%s\n' "$RENDERED"
