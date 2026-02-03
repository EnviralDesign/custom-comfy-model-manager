## ComfyUI Model Library Manager — Phase 2 Spec (v0.3, LOCKED)

> **Important:** Phase 2 is **not to be implemented until Phase 1 is complete and stable**.
> Phase 2 adds a “remote bootstrapper” workflow so you can spin up an Ubuntu GPU box, paste a small script, and remotely drive setup + downloads from your home Windows machine.

---

# 0) Phase 2 Goals

### What Phase 2 adds

1. New SPA page `/remote` to **open a temporary remote session** (a “pipe out”) and display a **fresh API key** each time.
2. A remote bootstrapper script (Ubuntu) that:

   * has a constant `BASE_URL` (hardcoded/configured once, unchanging)
   * has a constant `API_KEY` (fresh each session; pasted manually)
   * connects back to the home app over **HTTPS**
3. Remote-driven actions:

   * minimal “Install ComfyUI” scaffold (git clone into a folder)
   * queue downloads for **models** (and later also workflows/input images)
4. Tiered remote download sources:

   * public web URL (if known)
   * your home **Local** (fast) served by the app
   * your home **Lake** (slow) served by the app
5. Metadata store: **hash → optional web URL** to prioritize tier 1.

No access-policy / edge auth is required in this spec. **HTTPS + short-lived bearer API key** is the only auth.

---

# 1) Base URL (LOCKED)

### 1.1 Unchanging public URL

There is a single, stable URL that always points to the home app through your existing Cloudflare Zero Trust routing/tunnel configuration.

* The app reads it from env/config:

  * `REMOTE_BASE_URL=https://your.domain.example`
* The remote script contains the same constant:

  * `BASE_URL = "https://your.domain.example"`

This value does not change per session.

---

# 2) Remote Session Model (“Pipe Out”)

### 2.1 Default state

Remote endpoints are **OFF** by default. When OFF:

* all `/api/remote/*` endpoints reject requests
* all remote file streaming endpoints reject requests

### 2.2 Enabling the session

On `/remote`, user clicks:

**Button:** `Enable Remote Session`

The home app must:

* generate a fresh **API key** (high entropy)
* set an expiration time (TTL)
* enable a limited set of remote endpoints until TTL or manual end
* display the API key with a “Copy” button

**User workflow (LOCKED)**

* Paste the API key into the remote script constant near the top.
* Run the script on the Ubuntu GPU box.
* Script authenticates with `Authorization: Bearer <API_KEY>`.

### 2.3 Ending the session

Session ends when:

* user clicks `End Session`, OR
* TTL expires, OR
* app shuts down

On end:

* remote endpoints disabled immediately
* remote agent shown as disconnected
* any in-progress remote tasks will fail on next callback and must be restarted under a new session

### 2.4 Authentication (LOCKED)

All remote endpoints require:

* `Authorization: Bearer <API_KEY>`

API key:

* generated fresh per session
* not persisted beyond the session (except in UI display)
* invalid after TTL/end

---

# 3) Remote Bootstrapper Script (Ubuntu-only)

### 3.1 Script constraints

* Single file, minimal dependencies.
* Must run on **Ubuntu Linux**.
* Designed for copy/paste editing of a couple constants.

### 3.2 Script constants (LOCKED)

Near the top of the script:

* `BASE_URL = "https://your.domain.example"` (unchanging)
* `API_KEY = "PASTE_SESSION_KEY_HERE"` (fresh each session)
* `REMOTE_ROOT_DIR = "~/comfy_remote"` (default; configurable)

You may later sculpt/replace the script and likely leverage Astral UV; Phase 2 only needs a stable scaffold.

### 3.3 Responsibilities

1. Connect to `BASE_URL` over HTTPS with bearer token.
2. Register agent metadata.
3. Maintain a remote execution queue (default concurrency 1).
4. Execute tasks received from the home app:

   * `COMFY_GIT_CLONE` (scaffold install)
   * `ASSET_DOWNLOAD` (tiered download + resume + stall detect)
5. Report progress and logs back to the home app.
6. Heartbeat to keep session “alive” (optional but recommended).

### 3.4 Remote filesystem layout

Default layout under `~/comfy_remote/`:

* `ComfyUI/` (git clone destination)
* `models/` (models root)
* `workflows/` (future)
* `input/` (future)
* `downloads/.partial/` (resume temp files)
* `logs/`

---

# 4) Phase 2 UI: `/remote`

### 4.1 Session panel

* Status: OFF / ARMED / CONNECTED / RUNNING / ENDED
* Button: `Enable Remote Session`
* Displays:

  * `REMOTE_BASE_URL`
  * API key (fresh) + copy
  * session TTL countdown
* Button: `End Session`

### 4.2 Agent panel

When connected:

* hostname
* OS (Ubuntu)
* optional: python version, disk free
* heartbeat indicator
* live log stream

### 4.3 Actions panel

* `Install ComfyUI (Scaffold)` → queues `COMFY_GIT_CLONE`
* `Queue Model Downloads` → queue one or more `ASSET_DOWNLOAD` tasks
* Queue controls mirrored from Phase 1:

  * pause/resume/cancel remote queue items

### 4.4 Asset selection panel

Selection sources:

* browse/search your home model index (from Phase 1 indexing/hashing)
* add selected assets to a “remote cart”
* click `Queue Downloads`

---

# 5) Remote Task System

### 5.1 Task types

**A) ComfyUI install (scaffold, minimal)**

* `COMFY_GIT_CLONE`

  * Params:

    * `repo_url` (configurable)
    * `dest_path` (default `~/comfy_remote/ComfyUI`)
  * Behavior:

    * if dest_path absent: git clone repo_url dest_path
    * if dest_path exists: report “already exists” and do not modify (Phase 2 keeps this minimal)

**B) Asset download**

* `ASSET_DOWNLOAD`

  * Params:

    * `category`: `models` (Phase 2 baseline; workflows/input later)
    * `dest_relpath`: destination under remote category root (preserve relpath by default)
    * `sources[]`: ordered list of URLs to try
    * `expected_hash` (BLAKE3) if known (recommended)

### 5.2 Execution rules

* Remote concurrency default: 1
* Idempotent behavior:

  * if target file exists and expected_hash matches → skip
  * if exists and hash mismatch → rename existing file to `.conflict` and proceed (or fail; configurable later)
* Report progress continuously.

---

# 6) Tiered Download Source Resolution (LOCKED)

### 6.1 Source ordering

For every asset, the home app constructs sources in this order:

1. Public web URL (if present in metadata)
2. Home Local file stream URL (fast)
3. Home Lake file stream URL (slow)

Remote script logic:

* try sources in order
* retry per source
* on repeated failure/stall → fall back to next source

### 6.2 Source selection responsibility (LOCKED)

Home app decides and sends the ordered sources list.
Remote script does not “think” about where things live; it just tries the list.

---

# 7) Home App: Remote Endpoints + File Serving

### 7.1 Remote endpoints are session-gated

All `/api/remote/*` endpoints:

* require bearer token
* only active during session

### 7.2 Endpoints (Phase 2)

**Session**

* `POST /api/remote/session/enable`

  * returns: api_key, expires_at
* `POST /api/remote/session/end`

**Agent**

* `POST /api/remote/agent/register`
* `POST /api/remote/agent/heartbeat`

**Tasks**

* `POST /api/remote/tasks/enqueue` (from UI)
* `GET /api/remote/tasks/next` (long-poll fallback) OR websocket subscription
* `POST /api/remote/tasks/progress`
* `POST /api/remote/tasks/complete`
* `POST /api/remote/tasks/fail`

**Asset resolution**

* `POST /api/remote/assets/resolve`

  * input: hash or (side+relpath)
  * output: sources[] + expected_hash + dest_relpath

**Asset streaming (must support resume)**

* `GET /api/remote/assets/file?side=local|lake&relpath=...`

  * bearer required
  * supports `Range`
  * path traversal protection
* `GET /api/remote/assets/by-hash?hash=<blake3>&prefer=local_first`

  * bearer required
  * supports `Range`

### 7.3 Streaming requirements

* Efficient streaming for large files
* HTTP Range support
* Optional: server-side throttling / connection limits

---

# 8) Download Robustness Requirements

### 8.1 Resume

Remote script downloads to `.part` and resumes by Range:

* if server supports Range, resume from existing bytes
* if a source does not support resume, restart that source attempt (then fallback if needed)

### 8.2 Stall detection

Remote script detects stalled connections:

* if no bytes received for `STALL_TIMEOUT_SECONDS`, abort and retry
* after `RETRY_PER_SOURCE` attempts, fall back to next source tier

### 8.3 Hash verification

When expected_hash is provided:

* verify after download
* if mismatch:

  * mark failed and rename to `.badhash` (default)
  * optionally try next tier if available

---

# 9) Hash → URL Metadata Store (LOCKED)

### 9.1 Purpose

Allow associating a model’s hash with a public URL so remote provisioning can prefer tier 1.

### 9.2 Location & format

* Stored on Lake:

  * `<LAKE_MODELS_ROOT>\.model_sources.json`

Structure:

```json
{
  "<blake3_hash>": {
    "url": "https://…",
    "added_at": "2026-02-03T12:34:56Z",
    "notes": "optional",
    "filename_hint": "optional"
  }
}
```

### 9.3 UI editing

In `/sync` and/or `/dedupe`, for hashed files:

* Add/Edit/Remove “Source URL”

---

# 10) Bundles / Manifests (Recommended)

### 10.1 Bundle definition

A “bundle” is a named set of assets to provision to a remote box.

* Stored in app data: `bundles/<name>.json`
* Contains list of assets (prefer by hash), optional url override, optional dest_relpath.

### 10.2 Bundle workflow

* Select models → “Save as bundle”
* On `/remote`, choose bundle → “Provision remote”
* App resolves tiered sources and enqueues download tasks.

---

# 11) Configuration (Phase 2, LOCKED)

Home app:

* `REMOTE_BASE_URL` (unchanging public HTTPS URL)
* `REMOTE_SESSION_TTL_MINUTES` (default e.g. 60)
* `REMOTE_STALL_TIMEOUT_SECONDS` (e.g. 45)
* `REMOTE_RETRY_PER_SOURCE` (e.g. 3)
* `REMOTE_MAX_CONNECTIONS` (optional)

ComfyUI git:

* `COMFY_REPO_URL` (configurable; default set by you)
* `COMFY_DEST_PATH` (default `~/comfy_remote/ComfyUI` on remote)

Remote script constants:

* `BASE_URL` (same as REMOTE_BASE_URL)
* `API_KEY` (paste per session)
* `REMOTE_ROOT_DIR`

---

# 12) Acceptance Criteria (Phase 2 DONE)

1. `/remote` enables a session and shows a fresh API key each time.
2. You paste the key into the script and run it on Ubuntu; app shows agent CONNECTED.
3. “Install ComfyUI (Scaffold)” successfully git clones to the remote directory.
4. You can queue one or more model downloads from your home library to remote.
5. Remote downloads:

   * try web URL first (when known)
   * fall back to Local, then Lake
   * support resume and stall detection
   * verify hash when expected_hash is known
6. Home app streams Local/Lake files over HTTPS with Range, only during session.
7. URL metadata (hash → url) can be edited and is used in tier selection.
8. Bundles can be saved and used to provision a remote box (recommended feature).

---

If you want next: I can merge Phase 1 v0.4 + Phase 2 v0.3 into a single “Master Spec” document that contains both sections cleanly (still clearly separated), so you can hand it to a dev as one file.
