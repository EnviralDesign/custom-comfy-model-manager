# TECH_STACK.md — ComfyUI Model Library Manager (Windows)

This document outlines the intended tech stack at a high level. It is intentionally light on folder/layout opinions.

---

## Goals Driving the Stack

- **Windows-first** local tool (your main environment)
- **Single-process localhost server + browser UI**
- **Low code/token footprint** (AI-assisted development friendly)
- **Strong fit for Phase 2** (temporary remote “pipe out” + file streaming + queue progress)
- **No production build requirements** (dev-style workflow is fine)

---

## Recommended Stack (Default)

### Backend (Primary)
- **Python 3.11+**
- **FastAPI**
  - REST endpoints for indexing, diff, queue, mirror, dedupe, remote session (Phase 2)
  - WebSocket endpoints for realtime progress/events (queue, hashing, scanning)
- **Uvicorn** (ASGI server)

### Frontend (Lightweight “HTML over the wire”)
- **Server-rendered HTML** (Jinja2 templates)
- **HTMX** for partial page updates (swap HTML fragments without a SPA)
- **Minimal vanilla JS** only where needed:
  - websocket client for progress UI
  - small UX niceties (keyboard shortcuts, focus handling, client-only filtering if desired)

### Styling
- **Single CSS file**
- Optional: **PicoCSS** (classless, minimal) or similarly tiny CSS baseline
  - Avoid heavy component frameworks unless proven necessary

---

## Persistence / Storage

### Local app state (recommended)
- **SQLite** (via Python stdlib `sqlite3` or a minimal ORM if desired)
  - file index cache (relpath, size, mtime)
  - hash cache (BLAKE3, keyed by relpath+size+mtime)
  - queue history + logs
  - dedupe scan results (optional cache)

Alternative (acceptable for earliest prototype):
- JSON/JSONL files in app data directory, migrated to SQLite later.

### Metadata store (Phase 2 requirement)
- Hash → optional public URL mapping stored on **Lake**:
  - `<LAKE_MODELS_ROOT>\.model_sources.json`

---

## Hashing & Transfers

### Hashing
- **BLAKE3** (fast, low collision risk)
- Asynchronous hashing workers
- Persistent cache to avoid re-hashing unchanged files

### Transfers
- Queue-based transfers (serial by default)
- File copy + folder copy expansion
- Mirror plans generate a list of copy/delete tasks
- Range-capable streaming for Phase 2 remote pulls

---

## Phase 2 Remote “Pipe Out”

### Session + Auth
- Remote endpoints are **disabled by default**
- When user clicks “Enable Remote Session”:
  - Generate a fresh **API key**
  - Show it in UI for manual paste into remote script
- Remote calls use:
  - **HTTPS**
  - `Authorization: Bearer <API_KEY>`
- `REMOTE_BASE_URL` is unchanging and configured via env/config

### Remote Agent
- Ubuntu-only bootstrapper script
- Long-lived control channel:
  - WebSocket preferred, long-poll fallback acceptable
- Remote tasks:
  - `COMFY_GIT_CLONE` (scaffold)
  - `ASSET_DOWNLOAD` (tiered source list + resume + stall detection)

---

## Developer Experience

### Python environment tool (recommended)
- **Astral UV** for managing Python installs + dependencies + running the app

### Running locally
- One command to start the app:
  - `uv run python -m app` (example)
- App serves:
  - UI at `http://127.0.0.1:<port>/`
  - API under `/api/*`
  - WebSocket under `/ws/*`

---

## Configuration (via env or config file)

Required (Phase 1):
- `LOCAL_MODELS_ROOT`
- `LAKE_MODELS_ROOT`
- `LOCAL_ALLOW_DELETE`
- `LAKE_ALLOW_DELETE`
- `QUEUE_CONCURRENCY`
- `QUEUE_RETRY_COUNT`
- `APP_DATA_DIR`

Required (Phase 2):
- `REMOTE_BASE_URL`
- `REMOTE_SESSION_TTL_MINUTES`
- `REMOTE_STALL_TIMEOUT_SECONDS`
- `REMOTE_RETRY_PER_SOURCE`
- `COMFY_REPO_URL` (configurable)

---

## Non-Goals

- No requirement for a production-grade frontend build pipeline.
- No requirement for React/Vite unless later UX needs justify it.
- No edge auth / access policy mechanisms in Phase 2 beyond HTTPS + bearer token + short TTL.