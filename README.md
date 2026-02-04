# ComfyUI Model Library Manager

A Windows localhost tool for managing ComfyUI models across Local (fast SSD) and Lake (NAS) storage.

## Features

- **Two-pane sync browser** - Browse Local and Lake side-by-side
- **Diff visualization** - See what's missing, same, or conflicting
- **Queue-based transfers** - Copy files with progress, pause/resume
- **Mirror folders** - Make target match source
- **Dedupe wizard** - Find and delete duplicate files by BLAKE3 hash
- **AI source lookup** - Grok + Civitai API assisted download URL discovery
- **Standalone downloader** - Aggressive resume for Civitai/Hugging Face downloads
- **Agent trace** - Tool-based Civitai/HF discovery with step-by-step trace

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
   uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8420
   ```

4. **Open browser:**
   http://localhost:8420

## Standalone Downloader

Run the dedicated downloader UI on its own port:

```
uv run uvicorn app.downloader_app:app --reload --host 127.0.0.1 --port 8421
```

Downloads are saved to your default Downloads folder and resume aggressively on stalls.
The downloader UI also includes an **AI Agent Trace** panel for tool-based discovery.

## API Keys (Optional)

Set these in `.env` to enable authenticated downloads and richer lookup:

```
CIVITAI_API_KEY=
HUGGINGFACE_API_KEY=
XAI_API_KEY=
```

## Tech Stack

- **Backend:** FastAPI + Uvicorn
- **Frontend:** Jinja2, HTMX, vanilla JS
- **Database:** SQLite (via aiosqlite)
- **Hashing:** BLAKE3
