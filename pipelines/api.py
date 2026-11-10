"""FastAPI app: GET /health, POST /v1/explore and POST /v1/download (API key required)."""
import hmac

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import config
from .errors import PipelineError
from .explore import explore
from .export import export
from .models import DownloadRequest, ExploreRequest, load_overrides
from .transfer import from_edr

load_overrides()  # fail fast on a bad PAGINATION_OVERRIDES

app = FastAPI(title="Dataset pipelines", docs_url=None, redoc_url=None, openapi_url=None)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["POST"],
        allow_headers=["Content-Type", "X-Pipelines-Key"],
        expose_headers=["Content-Disposition", "X-Export-Warning"],
    )

NO_STORE = {"Cache-Control": "no-store, private"}


def require_key(x_pipelines_key: str | None = Header(default=None, alias="X-Pipelines-Key")):
    given = (x_pipelines_key or "").encode()
    # Check every key so timing doesn't reveal which one matched; no key configured = refuse.
    ok = False
    for key in config.API_KEYS:
        ok |= hmac.compare_digest(given, key.encode())
    if not given or not ok:
        raise PipelineError(401, "unauthorized", "Missing or invalid API key.")


@app.exception_handler(PipelineError)
async def _pipeline_error(_: Request, exc: PipelineError):
    return JSONResponse(status_code=exc.status, content=exc.body(), headers=NO_STORE)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    # Only locations and rules; never echo input (it may contain the token).
    errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "rule": e["msg"]} for e in exc.errors()]
    if any(f["field"].startswith(("schema", "format")) for f in errors):
        body = {"code": "invalid_schema", "message": "Download request is invalid. Re-run explore."}
    else:
        body = {"code": "invalid_transfer", "message": "Transfer is invalid."}
    return JSONResponse(status_code=400, content={**body, "detail": {"errors": errors}}, headers=NO_STORE)


@app.exception_handler(Exception)
async def _unexpected(_: Request, exc: Exception):
    return JSONResponse(status_code=500, headers=NO_STORE, content={
        "code": "internal_error", "message": "Something went wrong.", "detail": {"error": type(exc).__name__},
    })


@app.get("/health")
def health():
    return {"status": "ok"}


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_key)])


@v1.post("/explore")
def explore_endpoint(req: ExploreRequest):
    return JSONResponse(explore(from_edr(req.edr)), headers=NO_STORE)


@v1.post("/download")
def download_endpoint(req: DownloadRequest):
    result = export(from_edr(req.edr), req.format, req.schema_)
    headers = {**NO_STORE, "Content-Disposition": f'attachment; filename="{result.filename}"'}
    if result.warning:
        headers["X-Export-Warning"] = result.warning
    return Response(content=result.content, media_type=result.media_type, headers=headers)


app.include_router(v1)
