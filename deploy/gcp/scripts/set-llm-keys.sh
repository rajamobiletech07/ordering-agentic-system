#!/usr/bin/env bash
# Add the Claude / OpenAI / Gemini API keys to Secret Manager as new secret versions. Keys are read
# from a hidden prompt (never argv or shell history) and never pass through Terraform state.
#
#   deploy/gcp/scripts/set-llm-keys.sh <project-id>
#
# The llm-gateway pods mount all three, so each needs a version before the first deploy. Every
# route falls back across providers, so a key you leave empty ("unset") only makes that provider
# fail over.
# After rotating a key, restart the readers:  kubectl -n ordering rollout restart deploy/llm-gateway
set -euo pipefail

PROJECT="${1:?usage: set-llm-keys.sh <project-id>}"

for pair in "ordering-anthropic-api-key:Anthropic (Claude)" "ordering-openai-api-key:OpenAI" "ordering-google-api-key:Google (Gemini)"; do
  secret="${pair%%:*}"; label="${pair#*:}"
  read -r -s -p "$label API key (leave empty to skip/keep): " key; echo
  if [[ -z "$key" ]]; then
    if ! gcloud secrets versions list "$secret" --project "$PROJECT" --filter=state=enabled --format='value(name)' | grep -q .; then
      printf 'unset' | gcloud secrets versions add "$secret" --project "$PROJECT" --data-file=- >/dev/null
      echo "  $secret: no key given - stored placeholder 'unset' (provider will fail over)"
    else
      echo "  $secret: kept current version"
    fi
    continue
  fi
  printf '%s' "$key" | gcloud secrets versions add "$secret" --project "$PROJECT" --data-file=- >/dev/null
  unset key
  echo "  $secret: new version added"
done
