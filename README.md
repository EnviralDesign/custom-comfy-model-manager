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
- **Remote bootstrapper** - Provision a fresh ComfyUI install over an authenticated HTTPS tunnel
- **Bundles** - Deploy model files, ComfyUI input files, and custom node packs together

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

When using the remote bootstrapper, keep `API_KEY = "PASTE_KEY_HERE"` in
`bootstrapper.py` to make it prompt for the temporary session key. After the
agent registers, it fetches `CIVITAI_API_KEY` and `HUGGINGFACE_API_KEY` from the
local manager over the authenticated Cloudflare HTTPS connection. If the keys
are not configured locally, the bootstrapper prompts for them as a fallback.

## Bundles and Remote Provisioning

Bundles are deployment recipes for rented GPU hosts and fresh ComfyUI installs.

- Add model files from the **Sync** page. They deploy to `ComfyUI/models/...`.
- Add workflows from **Bundles -> + Workflow**. Paths are relative to local
  `ComfyUI/user/default/workflows` and deploy to the same remote workflow folder.
- Add workflow source files from **Bundles -> + Input Files**. Paths are relative
  to local `ComfyUI/input` and deploy to remote `ComfyUI/input/...`.
- Add custom node packs from **Bundles -> + Custom Node**. Registry installs use
  Comfy CLI so native ComfyUI Manager can recognize/manage them. Git URLs are
  supported as a fallback for packs missing from the registry.

Remote flow:

1. Start a remote session from the **Remote** page.
2. Run `bootstrapper.py` on the remote host and paste the session key when asked.
3. Queue **Run All Setup** to clone ComfyUI, create the venv, install PyTorch,
   install requirements, and enable native ComfyUI Manager.
4. Select bundles and click **Provision Selected Bundles**.

Provisioning installs custom node packs first, then downloads workflows, input
files, and finally heavier model files. Local model/input/workflow files are
streamed through the manager using the session bearer token; public source URLs
are used when available.

Local transfer tuning:

```
REMOTE_STREAM_CHUNK_MIB=4     # home app file-stream read size
DOWNLOAD_CHUNK_MIB=1          # bootstrapper read/write chunk size for responsiveness
DOWNLOAD_SEGMENTS=4           # parallel byte ranges for one large file, when supported
DOWNLOAD_SEGMENT_MIN_MIB=64   # only segment files at least this large
LOCAL_DOWNLOAD_WORKERS=1      # parallel local files; keep 1 for max per-file clarity
HF_DOWNLOAD_WORKERS=1         # parallel Hugging Face files; keep 1 for max per-file clarity
CIVITAI_DOWNLOAD_WORKERS=1    # parallel Civitai files; keep 1 for max per-file clarity
OTHER_DOWNLOAD_WORKERS=1      # parallel generic URL files; keep 1 for max per-file clarity
```

The default path favors single-file throughput and readable progress. For large
files from sources that support HTTP Range, the bootstrapper downloads multiple
byte ranges concurrently and assembles them locally. Raise the worker counts only
when you want higher aggregate batch throughput across several files.

## Tech Stack

- **Backend:** FastAPI + Uvicorn
- **Frontend:** Jinja2, HTMX, vanilla JS
- **Database:** SQLite (via aiosqlite)
- **Hashing:** BLAKE3
