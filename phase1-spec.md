## ComfyUI Model Library Manager — Phase 1 Spec (v0.4, LOCKED)

### 0) Purpose

Build a **Windows-only** tool that runs as a **localhost server + browser SPA** to manage a large ComfyUI **`models`** library across two storage locations:

* **Local (fast working set)**: USB NVMe SSD
* **Lake (datalake)**: Unraid NAS share

Phase 1 delivers two complete workflows:

1. **Sync & Mirror** between Local and Lake using a two-pane diff UI and a queued transfer engine.
2. **De-duplicate** within a single side using content hashing, via a wizard that **always allows deletion** (dedupe ignores allow-delete policy).

Phase 2 (remote bootstrapper + on-demand REST API) is **explicitly out of scope** for Phase 1 implementation, but the Phase 1 architecture must not block it.

---

## 1) Definitions & Terms

### 1.1 Sides

Two configurable roots, both pointing directly to the ComfyUI **models folder**:

* `LOCAL_MODELS_ROOT` (e.g., `D:\ComfyUI\models`)
* `LAKE_MODELS_ROOT` (e.g., `Y:\ComfyUI\models` or UNC)

### 1.2 Relative Path (RelPath)

All internal identification is based on a file’s path relative to the models root, e.g.:

* `checkpoints\foo.safetensors`
* `loras\bar.safetensors`

### 1.3 Hash Identity

Content hash is the canonical identity of file bytes.

* Hash algorithm: **BLAKE3**
* Hash cache uses: `RelPath + size + mtime + hash`
* If size+mtime unchanged, reuse cached hash.

---

## 2) Phase 1 Scope

### 2.1 Sync Mode (Local ↔ Lake)

* Two-pane browse UI
* Diff states (missing / same / conflict)
* Copy (file + folder) in both directions
* **Folder mirror** (target matches source, with plan preview + safety)
* Delete operations (policy-controlled per side)
* Fast fuzzy search using in-memory indexes
* Queue-based transfer system (serial by default), with progress + controls

### 2.2 Dedupe Mode (per-side)

* Scan **one side at a time** (Local or Lake)
* Find duplicates by full BLAKE3 hash
* Wizard to select “keep one” per duplicate set
* **Hard-delete duplicates from existence**
* **Dedupe deletion ignores allow-delete policy** (always permitted)
* Clear confirmations + summary

---

## 3) UI / UX Requirements

### 3.1 SPA Pages

The app is a single SPA with two primary routes:

* `/sync` — two-pane sync + mirror + queue
* `/dedupe` — scan + wizard + delete duplicates

Shared UI language (consistent style), but pages are distinct and optimized for each workflow.

---

## 4) `/sync` — Two-Pane Sync & Mirror

### 4.1 Layout

* Left pane: **Local**
* Right pane: **Lake**
* Each pane shows:

  * Folder tree navigation
  * File list for current folder
* Global top bar:

  * Fuzzy search input (filters both panes)
  * Refresh controls (global + per-side)
  * Queue widget (status + progress + controls)
  * Settings link

### 4.2 Diff Model & Visual States

Diff matches entries by **RelPath**.

Each RelPath is categorized:

1. **Only on Local**
2. **Only on Lake**
3. **On both, verified same**

   * Preferred: hash equal
   * If hash pending: “probable same” when size+mtime match
4. **Conflict: on both, RelPath same but hash differs**

   * Must show **yellow warning triangle**
   * Tooltip: “Warning: same relative path but different content (hash mismatch). Manual resolution required.”

#### Conflict behavior (LOCKED)

* Conflicts are **not overwriteable** by the tool.
* No “copy as new name” helper.
* Conflict rows expose only:

  * Open Local folder
  * Open Lake folder
  * Copy paths
  * View metadata

### 4.3 File & Folder Actions

Per item (file/folder), depending on state:

**Copy → other side**

* Allowed for items missing on the destination.
* For folders: creates a folder-copy task that expands into file copy tasks (preserve structure).
* If destination RelPath exists and is a conflict, copy must be blocked.

**Delete**

* Controlled by allow-delete flags:

  * `LOCAL_ALLOW_DELETE`
  * `LAKE_ALLOW_DELETE`
* If disabled for a side: delete actions are hidden/disabled in `/sync`.
* If enabled:

  * Confirmation required
  * Lake can have stronger confirmation (configurable, e.g. “type DELETE”).

**Utilities**

* Open in Explorer
* Copy full path / RelPath
* View metadata (size, mtime, hash status)

---

## 5) Mirror Feature (LOCKED)

### 5.1 Definition

A **Mirror** operation makes a target folder’s contents match a source folder’s contents **by RelPath**.

Inputs:

* Source side + source folder RelPath
* Target side + target folder RelPath

### 5.2 Mirror Plan Categories

Generate a plan with these buckets:

1. **Copy Missing**

* Exists in source, not in target → enqueue copy

2. **Delete Extras** (only if target side deletion allowed)

* Exists in target, not in source → propose delete

3. **Conflicts**

* Exists on both at same RelPath but hash differs → do not overwrite, mark as conflict

### 5.3 Mirror UX

* User clicks “Mirror →” (or “← Mirror”)
* App generates a **Mirror Plan Preview**:

  * Number of files to copy + total bytes
  * Number of files to delete + total bytes (only if allowed)
  * Number of conflicts (always)
* User must confirm to enqueue.

Mirror toggles in preview:

* `Skip deletes` (default ON if user wants safer; recommended default: OFF only when deletion is allowed and user explicitly enables)
* Conflict handling:

  * Default: **Skip conflicts and continue**
  * Option: Stop on first conflict (nice-to-have)

### 5.4 Mirror Safety Rules

* Never overwrite conflicts.
* If target deletion is disabled:

  * Mirror becomes “Additive mirror”: copy missing only; do not delete extras; still reports extras + conflicts.

---

## 6) Transfer Queue (LOCKED)

### 6.1 Queue Fundamentals

All filesystem-changing operations enqueue tasks:

* Copy file
* Copy folder (expanded)
* Delete file/folder (sync-only, policy gated)
* Mirror execution (creates many copy/delete tasks)

### 6.2 Execution Model

* Default `QUEUE_CONCURRENCY = 1` (serial transfers like TeraCopy)
* UI controls:

  * Pause / Resume
  * Cancel current item
  * Remove pending item(s)

### 6.3 Reliability

* Retry transient errors (configurable retry count)
* Clear failure states with actionable errors
* If Lake share disconnects mid-transfer: task fails with retry option; queue can pause automatically (nice-to-have)

### 6.4 Progress Reporting

Queue widget must show:

* Current task name + side direction
* Bytes copied / total
* Speed
* Error/retry count if applicable

Realtime updates via websocket.

---

## 7) Search & Indexing (LOCKED)

### 7.1 In-memory index

Maintain an in-memory index per side for fast filtering:

* RelPath
* filename
* extension
* size
* mtime
* hash status (unknown/pending/known)
* diff status

Search must feel “blazingly fast” and never hit disk per keystroke.

### 7.2 Refresh Strategy

* On startup: index both sides
* After any queue operation completes: update index (delta if possible)
* Manual “Refresh Index” button always available
* Filesystem watchers: optional later; default OFF

---

## 8) Hashing System (LOCKED)

### 8.1 Hashing runs async

Hash computation is asynchronous and should not block UI.

Use-cases requiring full hash:

* Dedupe scans (always)
* “Verified same” status in diff view (preferred)

### 8.2 Hash caching

Store hash results persistently in app data:

* If `size+mtime` unchanged, reuse hash
* If changed, recompute

---

## 9) `/dedupe` — Duplicate Detection & Wizard (LOCKED)

### 9.1 Scan

User selects target side:

* Local OR Lake
  Then “Scan for duplicates”

Scanner:

* Walk all files under models root
* Compute or reuse full BLAKE3 hash
* Group by hash where group size ≥ 2

Progress UI required.

### 9.2 Wizard

For each duplicate group:

* Display group index “i of N”
* List all files with:

  * RelPath
  * size
  * modified time
* User selects **one** file to keep
* Others marked delete

Final review screen:

* Total files to delete
* Estimated reclaimed space

### 9.3 Deletion Rules (LOCKED)

* Dedupe deletes are **hard deletes**.
* Dedupe deletion **ignores allow-delete flags** (always allowed).
* Must require final confirmation including:

  * side (Local/Lake)
  * number of files
  * total bytes to be deleted

---

## 10) Configuration (LOCKED)

### 10.1 Required keys

Paths:

* `LOCAL_MODELS_ROOT`
* `LAKE_MODELS_ROOT`

Deletion policy (sync only):

* `LOCAL_ALLOW_DELETE` (true/false)
* `LAKE_ALLOW_DELETE` (true/false)

Queue:

* `QUEUE_CONCURRENCY` (default 1)
* `QUEUE_RETRY_COUNT` (default 3)

Hashing:

* `HASH_ALGO=blake3`
* `HASH_WORKERS` (default 1–2)

App data:

* `APP_DATA_DIR` (default `%APPDATA%\ComfyModelManager`)

---

## 11) Backend/Frontend Architecture (LOCKED)

### 11.1 Single Windows process

One process hosts:

* REST API
* Websocket for realtime events
* Indexer service + persistent cache
* Queue worker
* Hash worker(s)

### 11.2 Phase 1 API Surface

Index & diff:

* `POST /api/index/refresh` (side=local|lake|both)
* `GET /api/index` (side=..., query=..., filters=...)
* `GET /api/diff` (returns per-RelPath statuses, conflict flags)

Queue:

* `POST /api/queue/copy` (srcSide, srcRelPath, dstSide)
* `POST /api/queue/delete` (side, relPath) **policy gated**
* `GET /api/queue`
* `POST /api/queue/pause|resume|cancel`

Mirror:

* `POST /api/mirror/plan` (srcSide, srcRelFolder, dstSide)
* `POST /api/mirror/execute` (plan payload)

Dedupe:

* `POST /api/dedupe/scan` (side)
* `GET /api/dedupe/results` (side)
* `POST /api/dedupe/execute` (keepSelections) **always allowed**

Realtime websocket events:

* queue progress
* scan progress
* hash progress
* index refresh progress

---

## 12) Acceptance Criteria (Phase 1 DONE means)

1. Runs on Windows; opens SPA in browser; serves API on localhost.
2. Two roots configured; `/sync` shows both panes correctly.
3. Diff states correct; conflicts show yellow warning triangle + tooltip; no overwrite capability.
4. Copy file/folder enqueues tasks; serial transfer works; progress visible; pause/resume/cancel works.
5. Mirror generates a preview plan and executes:

   * copies missing
   * deletes extras only if allowed
   * skips conflicts
6. Search is instant on big libraries.
7. `/dedupe` scan finds duplicates by hash; wizard works; deletes redundant files (hard delete) regardless of allow-delete flags.