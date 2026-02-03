"""
ComfyUI Remote Bootstrapper
---------------------------
Run this on your remote Ubuntu/WSL machine to connect it to your home ComfyUI Model Manager.

Usage: 
  Edit BASE_URL and API_KEY.
  Run with uv: `uv run bootstrapper.py`

/// script
requires-python = ">=3.11"
dependencies = [
    "requests",
]
///
"""

import os
import sys
import time
import json
import socket
import platform
import subprocess
import hashlib
from pathlib import Path
from urllib.parse import urlparse

# --- CONFIGURATION (Paste from UI) ---
BASE_URL = "http://127.0.0.1:8420"  # Set to your home app URL
API_KEY = "PASTE_KEY_HERE"          # Set to your session key
remote_root_dir = "~/comfy_remote"  # Where to install things

# --- CONSTANTS ---
USER_AGENT = "ComfyRemoteAgent/0.1"
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
STALL_TIMEOUT = 45

# --- SETUP ---
REMOTE_ROOT = Path(os.path.expanduser(remote_root_dir)).resolve()
COMFY_DIR = REMOTE_ROOT / "ComfyUI"
MODELS_DIR = REMOTE_ROOT / "models"
LOGS_DIR = REMOTE_ROOT / "logs"

# --- IMPORTS ---
import requests

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": USER_AGENT
})

def log(msg, error=False):
    ts = time.strftime("%H:%M:%S")
    prefix = "❌" if error else "ℹ️"
    print(f"[{ts}] {prefix} {msg}")

def ensure_dirs():
    for d in [REMOTE_ROOT, MODELS_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

# --- API WRAPPERS ---

def register_agent():
    log(f"Registering agent with {BASE_URL}...")
    try:
        payload = {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "details": {
                "python": platform.python_version(),
                "cwd": str(os.getcwd())
            }
        }
        resp = session.post(f"{BASE_URL}/api/remote/agent/register", json=payload)
        resp.raise_for_status()
        log("✅ Agent registered successfully.")
    except Exception as e:
        log(f"Registration failed: {e}", error=True)
        sys.exit(1)

def get_next_task():
    try:
        resp = session.get(f"{BASE_URL}/api/remote/tasks/next")
        if resp.status_code == 200:
            return resp.json() # Returns Task or None
        return None
    except Exception as e:
        log(f"Polling error: {e}", error=True)
        return None

def update_progress(task_id, status, progress=None, message=None, error=None):
    payload = {"task_id": task_id, "status": status}
    if progress is not None: payload["progress"] = progress
    if message: payload["message"] = message
    if error: payload["error"] = error
    
    try:
        session.post(f"{BASE_URL}/api/remote/tasks/progress", json=payload)
    except:
        pass

# --- TASK HANDLERS ---

def handle_git_clone(task):
    payload = task.get('payload', {})
    repo_url = payload.get('repo_url', "https://github.com/comfyanonymous/ComfyUI.git")
    dest = payload.get('dest_path', str(COMFY_DIR))
    dest_path = Path(dest)

    update_progress(task['id'], "running", 0.0, f"Cloning {repo_url}...")
    
    if dest_path.exists() and (dest_path / ".git").exists():
        log("ComfyUI already exists. Skipping clone.")
        update_progress(task['id'], "completed", 1.0, "Already exists")
        return

    try:
        log(f"Cloning to {dest_path}...")
        # Simple subprocess call
        cmd = ["git", "clone", repo_url, str(dest_path)]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            log("Clone successful.")
            update_progress(task['id'], "completed", 1.0, "Cloned successfully")
        else:
            log(f"Clone failed: {stderr}", error=True)
            update_progress(task['id'], "failed", 0.0, "Git clone failed", error=stderr)
            
    except Exception as e:
        log(f"Git execution error: {e}", error=True)
        update_progress(task['id'], "failed", 0.0, str(e), error=str(e))

def download_from_source(url, dest_path, task_id, existing_size=0):
    headers = {}
    mode = 'wb'
    if existing_size > 0:
        headers['Range'] = f'bytes={existing_size}-'
        mode = 'ab'
        
    try:
        with session.get(url, headers=headers, stream=True, timeout=STALL_TIMEOUT) as r:
            r.raise_for_status()
            
            # Handle potential 416 (Range Not Satisfiable) if file is already complete?
            # Convention: if server returns 200 instead of 206, it ignored Range. Reset file.
            if existing_size > 0 and r.status_code == 200:
                mode = 'wb'
                existing_size = 0
            
            total_size = int(r.headers.get('content-length', 0)) + existing_size
            downloaded = existing_size
            
            log(f"Downloading to {dest_path.name} (Resuming from {existing_size})" if existing_size else f"Downloading {dest_path.name}")

            with open(dest_path, mode) as f:
                last_update = time.time()
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Throttle updates to ~1s
                        now = time.time()
                        if now - last_update > 1.0:
                            pct = downloaded / total_size if total_size else 0
                            update_progress(task_id, "running", pct, f"Downloading: {int(pct*100)}%")
                            last_update = now
            
            return True, None
    except Exception as e:
        return False, str(e)

def handle_download(task):
    payload = task.get('payload', {})
    file_hash = payload.get('hash')
    relpath = payload.get('relpath')
    
    update_progress(task['id'], "running", 0.0, "Resolving sources...")
    
    # 1. Resolve sources via App
    try:
        resolve_resp = session.post(f"{BASE_URL}/api/remote/assets/resolve", 
                                  params={"hash": file_hash, "relpath": relpath})
        resolve_resp.raise_for_status()
        resolution = resolve_resp.json()
    except Exception as e:
        log(f"Resolution failed for {relpath}: {e}", error=True)
        update_progress(task['id'], "failed", 0.0, "Resolution failed", error=str(e))
        return

    # 2. Determine destination
    # Default to MODELS_DIR/relpath, but respect payload overrides if we had them
    # For now, just map relpath into local models structure
    if not resolution.get('relpath'):
        update_progress(task['id'], "failed", 0.0, "No relative path provided")
        return
        
    final_dest = MODELS_DIR / resolution['relpath']
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if exists (Simple check, not verifying hash in this iteration)
    if final_dest.exists():
        log(f"File {final_dest.name} already exists. Skipping.")
        update_progress(task['id'], "completed", 1.0, "Already exists")
        return

    temp_dest = final_dest.with_suffix(final_dest.suffix + ".part")
    
    # 3. Try sources in order
    sources = resolution.get('sources', [])
    if not sources:
        update_progress(task['id'], "failed", 0.0, "No download sources found")
        return

    success = False
    
    for src in sources:
        url = src['url']
        log(f"Trying source ({src['type']}): {url}")
        
        # Resume support logic
        current_size = 0
        if temp_dest.exists():
            current_size = temp_dest.stat().st_size
            
        ok, err = download_from_source(url, temp_dest, task['id'], current_size)
        
        if ok:
            temp_dest.rename(final_dest)
            success = True
            break
        else:
            log(f"Source failed: {err}", error=True)
            # Continue to next source
            
    if success:
        log(f"Download complete: {final_dest.name}")
        update_progress(task['id'], "completed", 1.0, "Download complete")
    else:
        log("All sources failed.", error=True)
        update_progress(task['id'], "failed", 0.0, "All sources failed")

# --- MAIN LOOP ---

def main():
    if API_KEY == "PASTE_KEY_HERE":
        print("Please edit the script and paste your API_KEY.")
        return

    ensure_dirs()
    register_agent()
    
    log("Waiting for tasks (Ctrl+C to stop)...")
    
    while True:
        try:
            # 1. Heartbeat
            session.post(f"{BASE_URL}/api/remote/agent/heartbeat")
            
            # 2. Get Task (with long-polling timeout on server side)
            task = get_next_task()
            if task:
                log(f"Received Task: {task['type']} ({task['id']})")
                
                if task['type'] == 'COMFY_GIT_CLONE':
                    handle_git_clone(task)
                elif task['type'] == 'ASSET_DOWNLOAD':
                    handle_download(task)
                else:
                    log(f"Unknown task type: {task['type']}")
                    update_progress(task['id'], "failed", error="Unknown task type")
            
            if not task:
                time.sleep(1) # Backup sleep if long poll returns fast
                
        except KeyboardInterrupt:
            log("Stopping agent.")
            break
        except Exception as e:
            log(f"Loop error: {e}", error=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
