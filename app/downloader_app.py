"""Standalone downloader UI/API app."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.routers import downloader as downloader_router
from app.routers import agent_debug as agent_debug_router
from app.routers import agent_tools as agent_tools_router

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

app = FastAPI(title="Comfy Downloader", version="0.1.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.get("/", response_class=HTMLResponse)
async def downloader_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse("downloader.html", {
        "request": request,
        "downloads_dir": str(settings.get_downloads_dir()),
    })


app.include_router(downloader_router.router)
app.include_router(agent_debug_router.router)
app.include_router(agent_tools_router.router)
