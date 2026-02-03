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
    
    # Start queue worker
    from app.services.worker import get_worker
    worker = get_worker()
    await worker.start()
    
    yield
    
    # Shutdown
    await worker.stop()
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


# ============================================================================
# API Routes (imported from routers)
# ============================================================================

from app.routers import index as index_router
from app.routers import queue as queue_router
from app.routers import dedupe as dedupe_router

app.include_router(index_router.router, prefix="/api/index", tags=["index"])
app.include_router(queue_router.router, prefix="/api/queue", tags=["queue"])
app.include_router(dedupe_router.router, prefix="/api/dedupe", tags=["dedupe"])

from app.routers import remote as remote_router
from app.routers import remote_assets
app.include_router(remote_router.router, prefix="/api/remote", tags=["remote"])
app.include_router(remote_assets.router, prefix="/api/remote", tags=["remote-assets"])


# ============================================================================
# WebSocket for realtime updates
# ============================================================================

from app.websocket import router as ws_router
app.include_router(ws_router)
