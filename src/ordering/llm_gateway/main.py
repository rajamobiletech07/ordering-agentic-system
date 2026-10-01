"""LLM Gateway service (FastAPI, port 8020).

Single entry point for all model access: authN (internal service tokens), tenant provider
policy, per-tenant budgets, routing + fallback (native) or LiteLLM passthrough, token/cost ledger.

Endpoints (OpenAI-compatible where it matters):
  POST /v1/chat/completions   - ``model`` = route name, or ``provider/model`` for a direct probe
  POST /v1/embeddings
  GET  /v1/models             - routes, their 3-model chains, provider health
  POST /v1/models/probe       - call every configured model once (admin/ops)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request

from ordering.common.config import get_settings
from ordering.common.context import Principal, RequestContext, set_context
from ordering.common.db import dispose_engines
from ordering.common.errors import AuthenticationError
from ordering.common.http import close_http_client
from ordering.common.logging import configure_logging, get_logger
from ordering.common.metrics import metrics_response
from ordering.common.tracing import setup_tracing
from ordering.llm_gateway.backends.litellm import LiteLLMBackend
from ordering.llm_gateway.backends.native import NativeBackend
from ordering.llm_gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    EmbeddingRequest,
    EmbeddingResponse,
)
from ordering.llm_gateway.usage import check_budget
from ordering.security.service_tokens import verify_service_token
from ordering.web import install_error_handlers, install_request_context

log = get_logger("ordering.llm.gateway")


def build_backend():
    settings = get_settings()
    return LiteLLMBackend(settings) if settings.llm_backend == "litellm" else NativeBackend(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.backend = build_backend()
    log.info("llm_gateway_started", backend=app.state.backend.name,
             override=get_settings().llm_provider_override or None)
    yield
    await close_http_client()
    await dispose_engines()


settings = get_settings()
settings.service_name = "llm-gateway"
configure_logging("llm-gateway", settings.log_level)
app = FastAPI(title="LLM Gateway", version=settings.agent_version, lifespan=lifespan)
install_request_context(app, "llm-gateway")
install_error_handlers(app)
setup_tracing(settings, app=app)


async def service_principal(request: Request, authorization: str = Header(default="")) -> Principal:
    if not authorization:
        raise AuthenticationError("missing service token")
    principal = verify_service_token(authorization)
    ctx = request.state.ctx
    set_context(RequestContext(request_id=ctx.request_id, principal=principal, agent=principal.actor,
                               conversation_id=ctx.conversation_id))
    return principal


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request):
    return {"status": "ready", "backend": request.app.state.backend.name, "providers": request.app.state.backend.health()}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           principal: Principal = Depends(service_principal)):
    await check_budget(principal.tenant_id)
    return await request.app.state.backend.chat(body, principal.tenant_id)


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
async def embeddings(body: EmbeddingRequest, request: Request, principal: Principal = Depends(service_principal)):
    await check_budget(principal.tenant_id)
    return await request.app.state.backend.embed(body, principal.tenant_id)


@app.get("/v1/models")
async def models(request: Request, _: Principal = Depends(service_principal)):
    backend = request.app.state.backend
    cfg = backend.config
    return {
        "backend": backend.name,
        "route_config_version": cfg.version,
        "routes": {name: [{"provider": m.provider, "model": m.model, **m.options} for m in r.models]
                   for name, r in cfg.routes.items()},
        "embeddings": {k: {"provider": v.provider, "model": v.model, "dims": v.dims} for k, v in cfg.embeddings.items()},
        "providers": backend.health(),
    }


@app.post("/v1/models/probe")
async def probe_models(request: Request, principal: Principal = Depends(service_principal)):
    """Call every configured model once with a tiny prompt; reports reachability per model."""
    backend = request.app.state.backend
    results = []
    for target in backend.config.all_models():
        req = ChatCompletionRequest(model=f"{target.provider}/{target.model}", max_tokens=16,
                                    messages=[ChatMessage(role="user", content="Reply with the single word: pong")])
        try:
            resp = await backend.chat(req, principal.tenant_id)
            results.append({"provider": target.provider, "model": target.model, "ok": True,
                            "reply": resp.choices[0].message.content[:40], "latency_ms": resp.x_gateway.attempts[-1].latency_ms})
        except Exception as exc:
            results.append({"provider": target.provider, "model": target.model, "ok": False,
                            "error": getattr(exc, "details", {}).get("attempts", [{}])[-1].get("error", str(exc))[:200]
                            if hasattr(exc, "details") else str(exc)[:200]})
    return {"results": results, "reachable": sum(r["ok"] for r in results), "total": len(results)}
