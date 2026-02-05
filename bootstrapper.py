# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests",
# ]
# ///

"""
ComfyUI Remote Bootstrapper
---------------------------
Run this on your remote Ubuntu/WSL machine to connect it to your home ComfyUI Model Manager.

Usage: 
  Edit BASE_URL and API_KEY.
  Run with uv: `uv run bootstrapper.py`


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
BASE_URL = "https://dl.enviral-design.com"  # Set to your home app URL
API_KEY = "PASTE_KEY_HERE"          # Set to your session key
HF_API_KEY = os.environ.get("HF_API_KEY", "").strip()
CIVITAI_API_KEY = os.environ.get("CIVITAI_API_KEY", "").strip()
TORCH_INDEX_URL = os.environ.get("TORCH_INDEX_URL", "").strip()
TORCH_INDEX_FLAG = os.environ.get("TORCH_INDEX_FLAG", "--extra-index-url").strip()
TORCH_PACKAGES = os.environ.get("TORCH_PACKAGES", "torch torchvision torchaudio").split()
remote_root_dir = "~/comfy_remote"  # Where to install things
BASE_HOST = urlparse(BASE_URL).netloc.lower()

# --- CONSTANTS ---
USER_AGENT = "ComfyRemoteAgent/0.1"
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
STALL_TIMEOUT = 45

# --- SETUP ---
REMOTE_ROOT = Path(os.path.expanduser(remote_root_dir)).resolve()
COMFY_DIR = REMOTE_ROOT / "ComfyUI"
# We will download directly into ComfyUI/models once cloned
MODELS_DIR = COMFY_DIR / "models" 

# --- IMPORTS ---
import requests

api_session = requests.Session()
api_session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": USER_AGENT
})

download_session = requests.Session()
download_session.headers.update({
    "User-Agent": USER_AGENT
})

def log(msg, error=False):
    ts = time.strftime("%H:%M:%S")
    prefix = "❌" if error else "ℹ️"
    print(f"[{ts}] {prefix} {msg}")

def ensure_dirs():
    # Only ensure root exists; ComfyUI dir is created by git clone
    REMOTE_ROOT.mkdir(parents=True, exist_ok=True)

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
        resp = api_session.post(f"{BASE_URL}/api/remote/agent/register", json=payload)
        resp.raise_for_status()
        log("✅ Agent registered successfully.")
    except Exception as e:
        log(f"Registration failed: {e}", error=True)
        sys.exit(1)

def get_next_task():
    try:
        resp = api_session.get(f"{BASE_URL}/api/remote/tasks/next")
        if resp.status_code == 200:
            return resp.json() # Returns Task or None
        return None
    except Exception as e:
        log(f"Polling error: {e}", error=True)
        return None

def update_progress(task_id, status, progress=None, message=None, error=None, meta=None):
    payload = {"task_id": task_id, "status": status}
    if progress is not None: payload["progress"] = progress
    if message: payload["message"] = message
    if error: payload["error"] = error
    if meta: payload["meta"] = meta
    
    try:
        api_session.post(f"{BASE_URL}/api/remote/tasks/progress", json=payload)
    except:
        pass

# --- HELPERS ---

def get_provider_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "unknown"
    if host.endswith("huggingface.co") or host.endswith("hf.co"):
        return "huggingface"
    if host.endswith("civitai.com"):
        return "civitai"
    return "unknown"

def auth_headers_for_source(provider: str, url: str) -> dict:
    if provider in {"local", "lake", "app"}:
        return {"Authorization": f"Bearer {API_KEY}"}

    host = None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = None

    if host and BASE_HOST and host == BASE_HOST:
        return {"Authorization": f"Bearer {API_KEY}"}

    if provider == "huggingface" and HF_API_KEY:
        return {"Authorization": f"Bearer {HF_API_KEY}"}
    if provider == "civitai" and CIVITAI_API_KEY:
        return {"Authorization": f"Bearer {CIVITAI_API_KEY}"}
    return {}

def get_venv_python() -> Path:
    venv_path = COMFY_DIR / ".venv"
    if platform.system().lower().startswith("win"):
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"

def run_cmd(cmd, cwd=None):
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr
    except FileNotFoundError as e:
        return 127, "", str(e)

def ensure_pip(task_id) -> tuple[bool, str]:
    venv_python = get_venv_python()
    if not venv_python.exists():
        return False, "Venv python not found."

    rc, _, err = run_cmd([str(venv_python), "-m", "pip", "--version"], cwd=COMFY_DIR)
    if rc == 0:
        return True, ""

    update_progress(task_id, "running", 0.0, "Bootstrapping pip...")
    rc, _, err = run_cmd([str(venv_python), "-m", "ensurepip", "--upgrade"], cwd=COMFY_DIR)
    if rc == 0:
        rc, _, err = run_cmd([str(venv_python), "-m", "pip", "--version"], cwd=COMFY_DIR)
        if rc == 0:
            return True, ""

    rc, _, err = run_cmd(["uv", "pip", "install", "--python", str(venv_python), "pip"], cwd=COMFY_DIR)
    if rc == 0:
        rc, _, err = run_cmd([str(venv_python), "-m", "pip", "--version"], cwd=COMFY_DIR)
        if rc == 0:
            return True, ""

    return False, err or "pip bootstrap failed."

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

def handle_create_venv(task):
    update_progress(task['id'], "running", 0.0, "Creating venv with uv (Python 3.13)...")
    
    # We run this INSIDE the ComfyUI directory
    if not COMFY_DIR.exists():
        update_progress(task['id'], "failed", 0.0, "ComfyUI directory not found. Install first.")
        return

    try:
        # Check if .venv already exists
        venv_path = COMFY_DIR / ".venv"
        if venv_path.exists():
            log("Venv already exists. Skipping.")
            update_progress(task['id'], "completed", 1.0, "Venv already exists")
            return

        cmd = ["uv", "venv", "--python", "3.13"]
        
        log(f"Running: {' '.join(cmd)} in {COMFY_DIR}")
        process = subprocess.Popen(
            cmd, 
            cwd=str(COMFY_DIR),
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True
        )
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            log("Venv created successfully.")
            ok, err = ensure_pip(task['id'])
            if ok:
                update_progress(task['id'], "completed", 1.0, "Venv created")
            else:
                update_progress(task['id'], "failed", 0.0, "Venv created but pip is missing", error=err)
        else:
            log(f"Venv creation failed: {stderr}", error=True)
            update_progress(task['id'], "failed", 0.0, "Venv creation failed", error=stderr)

    except Exception as e:
        log(f"Venv error: {e}", error=True)
        update_progress(task['id'], "failed", 0.0, str(e), error=str(e))

def handle_install_torch(task):
    payload = task.get('payload', {})
    packages = payload.get('packages') or TORCH_PACKAGES
    if isinstance(packages, str):
        packages = packages.split()

    index_url = payload.get('index_url') or TORCH_INDEX_URL
    index_flag = payload.get('index_flag') or TORCH_INDEX_FLAG or "--extra-index-url"

    if not COMFY_DIR.exists():
        update_progress(task['id'], "failed", 0.0, "ComfyUI directory not found. Install first.")
        return

    venv_python = get_venv_python()
    if not venv_python.exists():
        update_progress(task['id'], "failed", 0.0, "Venv not found. Create venv first.")
        return

    if not index_url:
        update_progress(task['id'], "failed", 0.0, "Torch index URL not set.")
        return

    ok, err = ensure_pip(task['id'])
    if not ok:
        update_progress(task['id'], "failed", 0.0, "pip is missing in venv", error=err)
        return

    update_progress(task['id'], "running", 0.0, "Upgrading pip...")
    rc, _, err = run_cmd([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task['id'], "failed", 0.0, "pip upgrade failed", error=err)
        return

    cmd = [str(venv_python), "-m", "pip", "install", *packages, index_flag, index_url]
    update_progress(task['id'], "running", 0.1, f"Installing PyTorch ({index_flag} {index_url})...")
    rc, _, err = run_cmd(cmd, cwd=COMFY_DIR)
    if rc == 0:
        update_progress(task['id'], "completed", 1.0, "PyTorch installed")
    else:
        update_progress(task['id'], "failed", 0.0, "PyTorch install failed", error=err)

def handle_install_requirements(task):
    if not COMFY_DIR.exists():
        update_progress(task['id'], "failed", 0.0, "ComfyUI directory not found. Install first.")
        return

    venv_python = get_venv_python()
    if not venv_python.exists():
        update_progress(task['id'], "failed", 0.0, "Venv not found. Create venv first.")
        return

    ok, err = ensure_pip(task['id'])
    if not ok:
        update_progress(task['id'], "failed", 0.0, "pip is missing in venv", error=err)
        return

    update_progress(task['id'], "running", 0.0, "Installing requirements.txt...")
    cmd = [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"]
    rc, _, err = run_cmd(cmd, cwd=COMFY_DIR)
    if rc == 0:
        update_progress(task['id'], "completed", 1.0, "Requirements installed")
    else:
        update_progress(task['id'], "failed", 0.0, "Requirements install failed", error=err)

def download_from_source(url, dest_path, task_id, existing_size=0, extra_headers=None):
    headers = {}
    if extra_headers:
        headers.update(extra_headers)
    mode = 'wb'
    if existing_size > 0:
        headers['Range'] = f'bytes={existing_size}-'
        mode = 'ab'
        
    try:
        with download_session.get(url, headers=headers, stream=True, timeout=STALL_TIMEOUT) as r:
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
        resolve_resp = api_session.post(f"{BASE_URL}/api/remote/assets/resolve", 
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
        provider = (src.get("provider") or get_provider_from_url(url)).lower()
        requires_auth = bool(src.get("requires_auth", False))
        if provider == "huggingface" and not HF_API_KEY:
            log("Skipping Hugging Face source (no HF key provided).")
            continue
        if provider == "civitai" and not CIVITAI_API_KEY:
            log("Skipping Civitai source (no Civitai key provided).")
            continue
        if requires_auth and provider == "unknown":
            log("Skipping source that requires auth (unknown provider).")
            continue

        log(f"Trying source ({src['type']}): {url}")
        
        # Resume support logic
        current_size = 0
        if temp_dest.exists():
            current_size = temp_dest.stat().st_size

        headers = auth_headers_for_source(provider, url)
        ok, err = download_from_source(url, temp_dest, task['id'], current_size, extra_headers=headers)
        
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

def handle_download_urls(task):
    payload = task.get('payload', {})
    items = payload.get('items', [])
    
    if not items:
        update_progress(task['id'], "completed", 1.0, "No items to download")
        return

    total_items = len(items)
    items_status = {}
    for i, item in enumerate(items):
        key = item.get('relpath') or item.get('url') or f"item_{i+1}"
        items_status[key] = "pending"

    update_progress(
        task['id'],
        "running",
        0.0,
        f"Starting batch download of {total_items} items...",
        meta={"items_status": items_status, "items_total": total_items, "items_done": 0}
    )
    done_count = 0

    for i, item in enumerate(items):
        relpath = item.get('relpath')
        url = item.get('url')
        file_hash = item.get('hash')
        item_key = relpath or url or f"item_{i+1}"
        
        if not relpath or not url:
            items_status[item_key] = "skipped"
            done_count += 1
            update_progress(
                task['id'],
                "running",
                done_count / total_items,
                f"Skipping {item_key} (missing data)",
                meta={"items_status": {item_key: "skipped"}, "items_done": done_count}
            )
            continue
            
        dest_path = MODELS_DIR / relpath
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Check if exists
        if dest_path.exists():
            # Simple size check could be added here
            log(f"[{i+1}/{total_items}] {relpath} already exists. Skipping.")
            items_status[item_key] = "skipped"
            done_count += 1
            update_progress(
                task['id'],
                "running",
                done_count / total_items,
                f"Skipping existing: {relpath}",
                meta={"items_status": {item_key: "skipped"}, "items_done": done_count}
            )
            continue

        log(f"[{i+1}/{total_items}] Downloading {relpath}...")
        items_status[item_key] = "downloading"
        update_progress(
            task['id'],
            "running",
            done_count / total_items,
            f"Downloading {i+1}/{total_items}: {relpath}",
            meta={"items_status": {item_key: "downloading"}, "items_done": done_count}
        )
        
        temp_dest = dest_path.with_suffix(dest_path.suffix + ".part")
        
        # Resume support logic (simple)
        current_size = 0
        if temp_dest.exists():
            current_size = temp_dest.stat().st_size

        provider = get_provider_from_url(url)
        if provider == "huggingface" and not HF_API_KEY:
            log(f"Skipping Hugging Face URL (no HF key provided): {relpath}")
            continue
        if provider == "civitai" and not CIVITAI_API_KEY:
            log(f"Skipping Civitai URL (no Civitai key provided): {relpath}")
            continue

        headers = auth_headers_for_source(provider, url)
        ok, err = download_from_source(url, temp_dest, task['id'], current_size, extra_headers=headers)
        
        if ok:
            temp_dest.rename(dest_path)
            log(f"Successfully downloaded {relpath}")
            items_status[item_key] = "completed"
        else:
            log(f"Failed to download {relpath}: {err}", error=True)
            # We continue with other items even if one fails
            items_status[item_key] = "failed"

        done_count += 1
        update_progress(
            task['id'],
            "running",
            done_count / total_items,
            f"Processed {i+1}/{total_items}: {relpath}",
            meta={"items_status": {item_key: items_status[item_key]}, "items_done": done_count}
        )
    
    update_progress(task['id'], "completed", 1.0, f"Batch download finished. Processed {total_items} items.")

# --- MAIN LOOP ---

def main():
    global API_KEY, HF_API_KEY, CIVITAI_API_KEY
    
    # Prompt for key if not baked in
    if API_KEY == "PASTE_KEY_HERE" or not API_KEY:
        print(f"Target: {BASE_URL}")
        try:
            # We use 'input' so you can see what you paste. 
            # Use getpass.getpass() if you prefer hidden input.
            val = input("Enter Session API Key: ").strip()
            if not val:
                print("No key provided. Exiting.")
                return
            API_KEY = val
            
            # Update session headers with the new key
            api_session.headers.update({"Authorization": f"Bearer {API_KEY}"})
            
        except KeyboardInterrupt:
            return

    if not HF_API_KEY:
        try:
            val = input("Enter Hugging Face API Key (optional, press Enter to skip): ").strip()
            if val:
                HF_API_KEY = val
        except KeyboardInterrupt:
            return

    if not CIVITAI_API_KEY:
        try:
            val = input("Enter Civitai API Key (optional, press Enter to skip): ").strip()
            if val:
                CIVITAI_API_KEY = val
        except KeyboardInterrupt:
            return

    ensure_dirs()
    register_agent()
    
    log("Waiting for tasks (Ctrl+C to stop)...")
    
    while True:
        try:
            # 1. Heartbeat
            api_session.post(f"{BASE_URL}/api/remote/agent/heartbeat")
            
            # 2. Get Task (with long-polling timeout on server side)
            task = get_next_task()
            if task:
                log(f"Received Task: {task['type']} ({task['id']})")
                
                if task['type'] == 'COMFY_GIT_CLONE':
                    handle_git_clone(task)
                elif task['type'] == 'CREATE_VENV':
                    handle_create_venv(task)
                elif task['type'] == 'ASSET_DOWNLOAD':
                    handle_download(task)
                elif task['type'] == 'DOWNLOAD_URLS':
                    handle_download_urls(task)
                elif task['type'] == 'PIP_INSTALL_TORCH':
                    handle_install_torch(task)
                elif task['type'] == 'PIP_INSTALL_REQUIREMENTS':
                    handle_install_requirements(task)
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
