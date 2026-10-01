# Ordering Agentic System - developer commands
PY ?= .venv/bin/python
COMPOSE ?= docker compose

.PHONY: help venv up down logs ps seed ingest ingest-all migrate token demo demo-failures test test-unit \
        test-integration test-e2e evals lint typecheck security openapi probe-models dev-up dev-down clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' Makefile | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

venv:            ## create .venv with pinned deps (uv)
	uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt -r requirements-dev.txt && uv pip install -p .venv -e . --no-deps

up:              ## build + start the full stack (migrate, seed, ingest run automatically)
	$(COMPOSE) up -d --build && $(PY) scripts/wait_for_services.py

down:            ## stop the stack (keeps data)
	$(COMPOSE) down

logs:            ## tail service logs
	$(COMPOSE) logs -f --tail=100 api-gateway agent-service llm-gateway mcp-ordering mcp-payment notification-worker

ps:
	$(COMPOSE) ps -a

migrate:         ## apply database migrations
	$(PY) -m alembic upgrade head

seed:            ## (re)seed dummy data - idempotent
	$(PY) -m scripts.seed

ingest:          ## ingest RAG documents for the configured embedding profile + offline profiles
	$(PY) -m ordering.rag.cli ingest --path data/rag_docs --profile "$${EMBEDDING_PROFILE:-openai},local,hashing"

ingest-all:      ## ingest for every embedding profile (needs OpenAI + Google keys unless mocked)
	$(PY) -m ordering.rag.cli ingest --path data/rag_docs --profile all

token:           ## print a dev JWT: make token USER_ID=u-acme-buyer
	@$(PY) -m scripts.issue_token $(or $(USER_ID),u-acme-buyer) --raw

demo:            ## scripted happy-path conversation
	$(PY) -m scripts.demo happy

demo-failures:   ## scripted failure-handling conversations
	$(PY) -m scripts.demo failures

test: test-unit test-integration test-e2e   ## all tests

test-unit:       ## unit tests (no infrastructure)
	$(PY) -m pytest tests/unit -q

test-integration: ## integration tests (needs: docker compose up -d postgres redis + migrate + seed)
	$(PY) -m pytest tests/integration -q -p no:logging

test-e2e:        ## end-to-end tests against the running stack
	$(PY) -m pytest tests/e2e -q -p no:logging

evals:           ## agent eval suites with baseline regression gate
	$(PY) -m ordering.agentops.evals.runner run --suite all

lint:
	$(PY) -m ruff check src tests scripts && $(PY) -m ruff format --check src tests scripts || true

typecheck:
	$(PY) -m mypy

security:        ## shift-left scans: SAST, dependency audit, secrets
	$(PY) -m bandit -q -r src -c pyproject.toml
	$(PY) -m pip_audit -r requirements.txt --progress-spinner off || true
	@command -v gitleaks >/dev/null && gitleaks detect --no-git -v || echo "gitleaks not installed (runs in CI)"

openapi:         ## export docs/openapi.json
	$(PY) scripts/export_openapi.py docs/openapi.json

probe-models:    ## call every configured LLM once (needs provider keys; admin token)
	@curl -s -X POST localhost:8020/v1/models/probe -H "Authorization: Bearer $$($(PY) -c 'from ordering.common.context import Principal; from ordering.security.service_tokens import mint_service_token; print(mint_service_token(Principal("ops","acme-retail",frozenset({"admin"})),"supervisor"))')" | $(PY) -m json.tool

dev-up:          ## run services as local processes (needs compose postgres + redis)
	scripts/dev_local.sh up

dev-down:
	scripts/dev_local.sh down

# ------------------------------------------------------------------ GCP / GKE (docs/deployment/gcp-gke.md)
KUBECONFORM = docker run --rm -v $(CURDIR)/.run/k8s:/work ghcr.io/yannh/kubeconform:v0.6.7 -strict -summary \
	-kubernetes-version 1.32.0 -schema-location default \
	-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
K8S_TEST_IMAGE = example.invalid/ordering-app@sha256:0000000000000000000000000000000000000000000000000000000000000000

k8s-render:      ## render staging + production manifests into .run/k8s/
	@mkdir -p .run/k8s
	deploy/k8s/render.sh staging $(K8S_TEST_IMAGE) > .run/k8s/staging.yaml
	deploy/k8s/render.sh production $(K8S_TEST_IMAGE) > .run/k8s/production.yaml

k8s-validate: k8s-render   ## schema-validate rendered manifests (kubeconform via Docker, incl. GKE CRDs)
	$(KUBECONFORM) /work/staging.yaml /work/production.yaml

tf-validate:     ## terraform fmt + validate (Docker, no cloud credentials)
	docker run --rm -v $(CURDIR)/deploy/gcp/terraform:/tf -w /tf hashicorp/terraform:1.13 fmt -check -recursive
	docker run --rm -v $(CURDIR)/deploy/gcp/terraform:/tf -w /tf -e TF_DATA_DIR=/tmp/.tf --entrypoint sh \
		hashicorp/terraform:1.13 -c "terraform init -backend=false -input=false >/dev/null && terraform validate"

iac-scan: k8s-render   ## Trivy IaC scan of rendered manifests + Terraform (same gate as terraform.yml)
	@mkdir -p .run/k8s/terraform && cp deploy/gcp/terraform/*.tf .run/k8s/terraform/
	docker run --rm -v $(CURDIR)/.run/k8s:/r -v $(CURDIR)/.trivyignore:/r/.trivyignore aquasec/trivy:0.58.1 \
		config --quiet --severity CRITICAL,HIGH,MEDIUM --ignorefile /r/.trivyignore --exit-code 1 /r

clean:
	rm -rf .run reports .pytest_cache .ruff_cache .mypy_cache
