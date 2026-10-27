"""
Laya Decision Model — FastAPI REST (Jev-compatible) + Streamable MCP server
Runs on ROCm (AMD GPU) inside Docker.

Jev-compatible endpoints
-------------------------
GET  /health                — GPU / device status (no auth)
GET  /v1/models             — list available checkpoints (no auth)
POST /v1/systemone          — single decision  (drop-in Jev replacement)
POST /v1/systemone/batch    — batch decisions  (Jev extension)
/mcp                        — Streamable HTTP MCP server

TypeSafe SDK compatibility
--------------------------
Point an existing TypeSafe SDK integration at this server by setting:
  TYPESAFE_BASE_URL=http://<host>:8000
  TYPESAFE_API_KEY=<LAYA_API_KEY>
No code changes needed.

Environment variables
---------------------
LAYA_API_KEY   : Bearer token clients must send.
                 If unset a random key is generated at startup and printed.
                 Matches TypeSafe SDK's TYPESAFE_API_KEY.
LAYA_MODEL     : Default checkpoint when the request omits the 'model' field.
                 Accepted values:
                   laya-multilingual    (default) — 322 M, 100+ languages, 1 024-tok ctx
                   laya-english                   — 421 M, English only,   512-tok ctx
                   laya-typed-decisions           — 421 M, English, workflow-tuned
                 Aliases accepted in both env var and per-request 'model' field:
                   laya | default | jev-latest  → laya-multilingual
HF_HOME        : Weight cache directory (default: /models)
"""

from __future__ import annotations

import contextlib
import os
import secrets
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp

from mcp.server.mcpserver import MCPServer, Context

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("laya")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# API key
LAYA_API_KEY: str = os.environ.get("LAYA_API_KEY", "")
if not LAYA_API_KEY:
    LAYA_API_KEY = secrets.token_urlsafe(32)
    log.warning("=" * 64)
    log.warning("No LAYA_API_KEY set — generated a random key:")
    log.warning("  LAYA_API_KEY=%s", LAYA_API_KEY)
    log.warning("Set TYPESAFE_API_KEY=<above> in your SDK / client config.")
    log.warning("=" * 64)

# Model aliases — keeps parity with Jev's model-name convention
_MODEL_ALIASES: dict[str, str] = {
    "laya":              "laya-multilingual",
    "default":           "laya-multilingual",
    "jev-latest":        "laya-multilingual",
    "multilingual":      "laya-multilingual",   # common shorthand
    "english":           "laya-english",
    "typed-decisions":   "laya-typed-decisions",
}

# Supported checkpoints (used for /v1/models response)
_SUPPORTED_MODELS = [
    {
        "id":          "laya-multilingual",
        "object":      "model",
        "description": "322 M params · mmBERT-base encoder · 100+ languages · 1 024-token context",
    },
    {
        "id":          "laya-english",
        "object":      "model",
        "description": "421 M params · ModernBERT-large encoder · English only · 512-token context",
    },
    {
        "id":          "laya-typed-decisions",
        "object":      "model",
        "description": "421 M params · ModernBERT-large encoder · English · workflow-tuned",
    },
]


def _resolve_model(name: str | None) -> str:
    """Resolve a model alias or name to the canonical checkpoint identifier."""
    if not name:
        raw = os.environ.get("LAYA_MODEL", "laya-multilingual")
    else:
        raw = name
    return _MODEL_ALIASES.get(raw, raw)


_DEFAULT_MODEL: str = _resolve_model(None)
log.info("Default checkpoint: %s", _DEFAULT_MODEL)

# ---------------------------------------------------------------------------
# Load Laya router (lazy — initialised in lifespan so errors surface via HTTP)
# ---------------------------------------------------------------------------
from laya import Router  # noqa: E402

router: Router | None = None
_startup_error: str | None = None   # set if Router() fails; reported by /health


def _run_decision(state: dict | str, questions: dict, model: str) -> dict:
    """
    Execute one Laya forward pass and return a Jev-shaped response:
      { model, answers, usage }
    Raises HTTPException on failure so the worker stays alive.
    """
    from fastapi import HTTPException
    if router is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": _startup_error or "Laya router not initialised",
                    "type":    "startup_error",
                }
            },
        )
    try:
        raw: dict = router.predict(state, questions)
    except Exception as exc:
        log.error("Router.predict failed (model=%s): %s", model, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": str(exc),
                    "type":    "inference_error",
                    "model":   model,
                }
            },
        )
    return {
        "model":   model,
        "answers": raw.get("answers", raw),
        "usage":   raw.get("usage", {"input_tokens": 0, "output_tokens": 0}),
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_PUBLIC_PATHS = {"/health", "/v1/models", "/", "/favicon.ico"}


def _check_auth(request: Request) -> bool:
    if request.url.path in _PUBLIC_PATHS:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return secrets.compare_digest(auth[len("Bearer "):], LAYA_API_KEY)
    return False


# ---------------------------------------------------------------------------
# MCP server (Streamable HTTP — stateless per request)
# ---------------------------------------------------------------------------
mcp = MCPServer("laya")


@mcp.tool()
def system_one(state: dict | str, questions: dict, model: Optional[str] = None) -> dict:
    """
    Run typed decisions (choice / score / noul) over a single state.
    Jev-compatible: same request/response shape as POST /v1/systemone.

    Parameters
    ----------
    state     : Input to decide over — free text, email, ticket, or JSON object.
    questions : Dict of question_name → question spec.
                Keys: type ('choice'|'score'|'noul'), instructions, criteria.
    model     : Optional checkpoint override. Defaults to server's LAYA_MODEL.
                Accepted: laya-multilingual | laya-english | laya-typed-decisions
                Aliases:  laya | default | jev-latest

    Returns
    -------
    { model, answers, usage }  — identical shape to POST /v1/systemone.
    """
    resolved = _resolve_model(model)
    return _run_decision(state, questions, resolved)


@mcp.tool()
async def system_one_batch(
    items: list[dict | str],
    questions: dict,
    ctx: Context,
    model: Optional[str] = None,
) -> list[dict]:
    """
    Run the same questions over a list of states, streaming progress as each item completes.
    Jev extension — mirrors POST /v1/systemone/batch.

    Parameters
    ----------
    items     : List of states (strings or JSON objects).
    questions : Shared question spec applied to every state.
    model     : Optional checkpoint override (same aliases as system_one).

    Returns
    -------
    List of { model, answers, usage } dicts in the same order as `items`.
    """
    resolved = _resolve_model(model)
    results: list[dict] = []
    total = len(items)
    for i, state in enumerate(items):
        results.append(_run_decision(state, questions, resolved))
        await ctx.report_progress(i + 1, total, f"Processed {i + 1}/{total}")
    return results


_mcp_asgi: ASGIApp = mcp.streamable_http_app(stateless_http=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    global router, _startup_error
    log.info("Initialising Laya Router (model=%s) …", _DEFAULT_MODEL)
    try:
        router = Router(default=_DEFAULT_MODEL)
        log.info("Laya router ready.")
    except Exception as exc:
        _startup_error = str(exc)
        log.error("=" * 64)
        log.error("Laya Router init FAILED — server will respond 503 to inference requests.")
        log.error("Error: %s", exc, exc_info=True)
        log.error("HSA_OVERRIDE_GFX_VERSION=%s", os.environ.get("HSA_OVERRIDE_GFX_VERSION", "<unset>"))
        log.error("=" * 64)
        # Do NOT re-raise — uvicorn stays alive so /health can report the error.
    # mcp v2 stateless HTTP: streamable_http_app() manages its own lifecycle.
    # No session_manager.run() needed — that was v1 only.
    yield


app = FastAPI(
    title="Laya Decision API  (Jev-compatible)",
    description=(
        "Drop-in Jev replacement powered by the open-weight Laya decision model on ROCm. "
        "Point your TypeSafe SDK at this server via `TYPESAFE_BASE_URL` + `TYPESAFE_API_KEY`."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _check_auth(request):
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Unauthorized — provide: Authorization: Bearer <LAYA_API_KEY>",
                    "type":    "authentication_error",
                    "code":    "invalid_api_key",
                }
            },
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic schemas  (Jev-compatible field names)
# ---------------------------------------------------------------------------

class SystemOneRequest(BaseModel):
    """
    Jev-compatible request body for POST /v1/systemone.

    `model` is optional; when omitted the server's LAYA_MODEL is used.
    Accepted model values: laya-multilingual | laya-english | laya-typed-decisions
    Aliases: laya | default | jev-latest
    """
    model:     Optional[str] = Field(default=None, examples=["laya-multilingual"])
    state:     dict | str    = Field(..., description="Input state: free text or JSON object")
    questions: dict          = Field(..., description="Dict of question_name → question spec")

    model_config = {
        "json_schema_extra": {
            "example": {
                "model": "laya-multilingual",
                "state": {
                    "subject": "Duplicate charge",
                    "body":    "Billed twice for March invoice #4411. Refund the duplicate today or we will cancel.",
                },
                "questions": {
                    "team": {
                        "type":         "choice",
                        "instructions": "Which team should handle this request?",
                        "criteria": {
                            "billing":   "invoices, payments, refunds",
                            "technical": "bugs, outages, errors",
                            "sales":     "pricing, new plans",
                            "other":     "everything else",
                        },
                    },
                    "churn":   {"type": "noul",  "instructions": "Does the customer explicitly threaten to cancel?"},
                    "urgency": {"type": "score", "instructions": "How urgent is this request?",
                                "criteria": ["not urgent", "soon", "today"]},
                },
            }
        }
    }


class SystemOneBatchRequest(BaseModel):
    """Batch extension of the Jev systemone API."""
    model:     Optional[str]    = Field(default=None, examples=["laya-multilingual"])
    items:     list[dict | str] = Field(..., description="List of states to decide over")
    questions: dict             = Field(..., description="Shared question spec applied to every state")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"], summary="GPU / liveness probe (no auth)")
def health():
    """Returns GPU + router status. Does not require authentication."""
    try:
        import torch
        cuda_ok     = torch.cuda.is_available()
        device_name = torch.cuda.get_device_name(0) if cuda_ok else None
        hip_version = getattr(torch.version, "hip", None)
    except Exception as exc:
        return {"status": "degraded", "error": str(exc)}

    overall = "ok" if (router is not None and cuda_ok) else "degraded"
    return {
        "status":               overall,
        "router_ready":         router is not None,
        "startup_error":        _startup_error,
        "default_model":        _DEFAULT_MODEL,
        "gpu_available":        cuda_ok,
        "device":               device_name,
        "rocm_hip":             hip_version,
        "torch":                __import__("torch").__version__,
        "hsa_override_gfx":     os.environ.get("HSA_OVERRIDE_GFX_VERSION"),
    }


@app.get("/v1/models", tags=["models"], summary="List available Laya checkpoints (no auth)")
def list_models():
    """
    Jev-compatible model listing.
    Returns the set of checkpoints this server can serve.
    """
    return {"object": "list", "data": _SUPPORTED_MODELS}


@app.post(
    "/v1/systemone",
    tags=["decisions"],
    summary="Single decision — Jev-compatible",
    response_description='{ "model": "...", "answers": { ... }, "usage": { ... } }',
)
def systemone(r: SystemOneRequest):
    """
    Jev-compatible endpoint.  Accepts the same request body and returns the same
    response shape as TypeSafe's `POST /v1/systemone`.

    **TypeSafe SDK drop-in**
    ```
    TYPESAFE_BASE_URL=http://<host>:8000
    TYPESAFE_API_KEY=<LAYA_API_KEY>
    ```
    No code changes needed in existing SDK integrations.
    """
    resolved = _resolve_model(r.model)
    return _run_decision(r.state, r.questions, resolved)


@app.post(
    "/v1/systemone/batch",
    tags=["decisions"],
    summary="Batch decisions — Jev extension",
    response_description="Array of { model, answers, usage } in input order",
)
def systemone_batch(r: SystemOneBatchRequest):
    """
    Run the same questions over a list of states.
    Returns results in the same order as `items`.

    This is an extension of the Jev API (Jev has no batch endpoint).
    The MCP `system_one_batch` tool streams progress; this REST endpoint
    returns all results at once.
    """
    resolved = _resolve_model(r.model)
    return [_run_decision(state, r.questions, resolved) for state in r.items]


# Mount MCP last — must not shadow the REST routes above.
app.mount("/mcp", _mcp_asgi)
