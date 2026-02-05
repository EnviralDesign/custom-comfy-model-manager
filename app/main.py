"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.database import startup_db

# Paths
APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    settings = get_settings()
    print(f"╔══════════════════════════════════════════════════════════════╗")
    print(f"║       ComfyUI Model Library Manager v0.1.0                   ║")
    print(f"╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Local: {str(settings.local_models_root):<52} ║")
    print(f"║  Lake:  {str(settings.lake_models_root):<52} ║")
    print(f"╠══════════════════════════════════════════════════════════════╣")
    print(f"║  UI: http://{settings.host}:{settings.port:<43} ║")
    print(f"╚══════════════════════════════════════════════════════════════╝")
    
    # Initialize database
    await startup_db()
    print("✓ Database initialized")

    # Reset any running tasks (server restarts leave them orphaned)
    from app.database import get_db
    async with get_db() as db:
        cursor = await db.execute(
            """
            UPDATE queue
            SET status = 'pending',
                started_at = NULL,
                bytes_transferred = 0,
                error_message = NULL
            WHERE status = 'running'
            """
        )
        await db.commit()
        if cursor.rowcount:
            print(f"↺ Reset {cursor.rowcount} running queue task(s) to pending")
    
    # Start queue worker
    from app.services.worker import get_worker
    worker = get_worker()
    await worker.start()

    # Start AI lookup worker
    from app.services.ai_lookup_worker import get_ai_lookup_worker
    ai_worker = get_ai_lookup_worker()
    await ai_worker.start()

    # Restore downloader jobs
    from app.services.downloader import get_download_manager
    await get_download_manager().load_persisted_jobs()
    
    yield
    
    # Shutdown
    await worker.stop()
    await ai_worker.stop()
    print("Shutting down...")


app = FastAPI(
    title="ComfyUI Model Manager",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ============================================================================
# Security Middleware (Split Horizon)
# ============================================================================

from urllib.parse import urlparse
from fastapi.responses import PlainTextResponse

@app.middleware("http")
async def filter_external_traffic(request: Request, call_next):
    """
    Split Horizon Security:
    - If request comes from the configured REMOTE_BASE_URL host (Tunnel),
      LOCK DOWN ACCESS. Only allow /api/remote/* endpoints.
      Block UI, Sync, Dedupe, and local management APIs.
    - If request comes from localhost/127.0.0.1, ALLOW ALL.
    """
    settings = get_settings()
    
    # 1. Identify if this is external traffic
    host_header = request.headers.get("host", "").split(":")[0]
    
    # Parse configured remote host (e.g. "comfy-remote.tunnels.com")
    try:
        remote_host = urlparse(settings.remote_base_url).hostname
    except:
        remote_host = None

    # If it matches the remote tunnel...
    if remote_host and host_header.lower() == remote_host.lower():
        # 2. Enforce Allowlist
        # We only allow the remote agent API.
        if not request.url.path.startswith("/api/remote"):
            # Start strict: 403 Forbidden for everything else
            return PlainTextResponse("Forbidden: External access restricted to Remote Agent API only.", status_code=403)

    return await call_next(request)


# ============================================================================
# HTML Routes (HTMX-friendly)
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect to sync page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    """Two-pane sync view."""
    settings = get_settings()
    return templates.TemplateResponse("sync.html", {
        "request": request,
        "local_root": str(settings.local_models_root),
        "lake_root": str(settings.lake_models_root),
        "local_allow_delete": settings.local_allow_delete,
        "lake_allow_delete": settings.lake_allow_delete,
    })


@app.get("/dedupe", response_class=HTMLResponse)
async def dedupe_page(request: Request):
    """Dedupe wizard view."""
    return templates.TemplateResponse("dedupe.html", {"request": request})


@app.get("/remote", response_class=HTMLResponse)
async def remote_page(request: Request):
    """Remote session management view."""
    return templates.TemplateResponse("remote.html", {"request": request})


@app.get("/bundles", response_class=HTMLResponse)
async def bundles_page(request: Request):
    """Bundles management view."""
    return templates.TemplateResponse("bundles.html", {"request": request})


@app.get("/ai-review", response_class=HTMLResponse)
async def ai_review_page(request: Request):
    """AI lookup review view."""
    return templates.TemplateResponse("ai_review.html", {"request": request})


# ============================================================================
# API Routes (imported from routers)
# ============================================================================

from app.routers import index as index_router
from app.routers import queue as queue_router
from app.routers import dedupe as dedupe_router
from app.routers import sources as sources_router
from app.routers import bundles as bundles_router
from app.routers import downloader as downloader_router

app.include_router(index_router.router, prefix="/api/index", tags=["index"])
app.include_router(queue_router.router, prefix="/api/queue", tags=["queue"])
app.include_router(dedupe_router.router, prefix="/api/dedupe", tags=["dedupe"])
app.include_router(sources_router.router, prefix="/api/index", tags=["sources"])
app.include_router(bundles_router.router, prefix="/api", tags=["bundles"])
app.include_router(downloader_router.router, prefix="/api", tags=["downloader"])
from app.routers import ai_lookup as ai_lookup_router
app.include_router(ai_lookup_router.router, prefix="/api/ai", tags=["ai-lookup"])

from app.routers import remote as remote_router
from app.routers import remote_assets
app.include_router(remote_router.router, prefix="/api/remote", tags=["remote"])
app.include_router(remote_assets.router, prefix="/api/remote", tags=["remote-assets"])


# ============================================================================
# WebSocket for realtime updates
# ============================================================================

from app.websocket import router as ws_router
app.include_router(ws_router)
