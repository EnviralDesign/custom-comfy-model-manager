# ComfyUI Model Library Manager

A Windows localhost tool for managing ComfyUI models across Local (fast SSD) and Lake (NAS) storage.

## Features

- **Two-pane sync browser** - Browse Local and Lake side-by-side
- **Diff visualization** - See what's missing, same, or conflicting
- **Queue-based transfers** - Copy files with progress, pause/resume
- **Mirror folders** - Make target match source
- **Dedupe wizard** - Find and delete duplicate files by BLAKE3 hash

## Quick Start

1. **Install dependencies:**
   ```
   uv pip install -e .
   ```

2. **Configure paths** in `.env`:
   ```
   LOCAL_MODELS_ROOT=D:\ComfyUI\models
   LAKE_MODELS_ROOT=Y:\ComfyUI\models
   ```

3. **Run the server:**
   ```
   uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8420
   ```

4. **Open browser:**
   http://127.0.0.1:8420

## Tech Stack

- **Backend:** FastAPI + Uvicorn
- **Frontend:** Jinja2, HTMX, vanilla JS
- **Database:** SQLite (via aiosqlite)
- **Hashing:** BLAKE3
