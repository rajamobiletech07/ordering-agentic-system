#!/usr/bin/env bash
# Start a one-off Job from a suspended CronJob template, stream its logs, and exit with its result.
#
#   deploy/k8s/run-job.sh db-migrate
#   deploy/k8s/run-job.sh ops-toolbox -- python -m ordering.prompts.rollouts set intent_classifier 1.1.0 10
#
# With `-- cmd...` the container command is overridden (the ops-toolbox pattern), so the command
# runs with the template's service account, secrets and network policy, and nothing is kubectl-exec'd.
set -euo pipefail

CRONJOB="${1:?usage: run-job.sh <cronjob> [timeout-seconds] [-- command...]}"
shift
TIMEOUT=900
if [[ $# -gt 0 && "$1" != "--" ]]; then TIMEOUT="$1"; shift; fi
NS="${NAMESPACE:-ordering}"
SUFFIX="${JOB_SUFFIX:-$(date -u +%Y%m%d%H%M%S)}"
JOB="${CRONJOB}-${SUFFIX}"
JOB="${JOB:0:63}"

if [[ "${1:-}" == "--" ]]; then
  shift
  CMD_JSON="$(printf '%s\n' "$@" | jq -R . | jq -s .)"
  kubectl -n "$NS" get cronjob "$CRONJOB" -o json \
    | jq --arg name "$JOB" --argjson cmd "$CMD_JSON" '{
        apiVersion: "batch/v1", kind: "Job",
        metadata: {name: $name, namespace: .metadata.namespace,
                   labels: {"app.kubernetes.io/part-of": "ordering", "ordering.io/from-cronjob": .metadata.name}},
        spec: (.spec.jobTemplate.spec | .template.spec.containers[0].command = $cmd)}' \
    | kubectl apply -f -
else
  kubectl -n "$NS" create job "$JOB" --from="cronjob/$CRONJOB"
fi

echo "job/$JOB started (timeout ${TIMEOUT}s)"
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  succeeded="$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  failed="$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.failed}' 2>/dev/null || true)"
  if [[ "${succeeded:-0}" -ge 1 ]]; then result=0; break; fi
  if kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' | grep -q True; then
    result=1; break
  fi
  if [[ $(date +%s) -ge $deadline ]]; then echo "timed out waiting for job/$JOB (failed pods: ${failed:-0})" >&2; result=1; break; fi
  sleep 5
done

kubectl -n "$NS" logs "job/$JOB" --all-containers=true --tail=500 || true
[[ $result -eq 0 ]] && echo "job/$JOB succeeded" || echo "job/$JOB FAILED" >&2
exit "$result"
