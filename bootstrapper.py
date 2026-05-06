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
  Interactive:
    uv run bootstrapper.py

  Disable Lightning Studio keep-awake:
    LIGHTNING_KEEPALIVE=0 uv run bootstrapper.py

  Non-interactive / Lightning Job:
    REMOTE_BASE_URL=https://example.com REMOTE_API_KEY=... COMFY_DIR=ComfyUI \
      PROMPT_OPTIONAL_KEYS=0 CREATE_COMFY_DIR=1 uv run bootstrapper.py


"""

import os
import sys
import time
import json
import socket
import platform
import subprocess
import hashlib
import threading
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

def env_int(name, default, minimum=None):
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


# --- CONFIGURATION ---
BASE_URL = os.environ.get("REMOTE_BASE_URL", "https://dl.enviral-design.com").strip()
API_KEY = os.environ.get("REMOTE_API_KEY", "PASTE_KEY_HERE").strip()
HF_API_KEY = os.environ.get("HF_API_KEY", "").strip()
CIVITAI_API_KEY = os.environ.get("CIVITAI_API_KEY", "").strip()
PROMPT_OPTIONAL_KEYS = os.environ.get("PROMPT_OPTIONAL_KEYS", "1").strip().lower() in {"1", "true", "yes", "y"}
CREATE_COMFY_DIR = os.environ.get("CREATE_COMFY_DIR", "0").strip().lower() in {"1", "true", "yes", "y"}
TORCH_INDEX_URL = os.environ.get("TORCH_INDEX_URL", "").strip()
TORCH_INDEX_FLAG = os.environ.get("TORCH_INDEX_FLAG", "--extra-index-url").strip()
TORCH_PACKAGES = os.environ.get("TORCH_PACKAGES", "torch torchvision torchaudio").split()
DOWNLOAD_MAX_RETRIES = max(1, int(os.environ.get("DOWNLOAD_MAX_RETRIES", "3")))
DOWNLOAD_RETRY_BACKOFF_SECONDS = max(0.0, float(os.environ.get("DOWNLOAD_RETRY_BACKOFF_SECONDS", "2")))
EXTRA_PIP_PACKAGES = [p for p in os.environ.get("EXTRA_PIP_PACKAGES", "sageattention triton").split() if p]
OPTIONAL_PIP_PACKAGES = [p for p in os.environ.get("OPTIONAL_PIP_PACKAGES", "flash-attn").split() if p]
TRANSFORMERS_COMPAT_PIN = os.environ.get("TRANSFORMERS_COMPAT_PIN", "transformers<5").strip()
LIGHTNING_KEEPALIVE = os.environ.get("LIGHTNING_KEEPALIVE", "1").strip().lower() in {"1", "true", "yes", "y"}
KEEPALIVE_INTERVAL_SECONDS = max(10.0, float(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "45")))
KEEPALIVE_BURST_SECONDS = max(0.5, float(os.environ.get("KEEPALIVE_BURST_SECONDS", "3")))
DOWNLOAD_CHUNK_MIB = env_int("DOWNLOAD_CHUNK_MIB", 1, minimum=1)
PROGRESS_UPDATE_INTERVAL_SECONDS = max(0.1, float(os.environ.get("PROGRESS_UPDATE_INTERVAL_SECONDS", "0.25")))
PROGRESS_POST_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("PROGRESS_POST_TIMEOUT_SECONDS", "5")))
DOWNLOAD_SEGMENTS = env_int("DOWNLOAD_SEGMENTS", 4, minimum=1)
DOWNLOAD_SEGMENT_MIN_MIB = env_int("DOWNLOAD_SEGMENT_MIN_MIB", 64, minimum=1)
DOWNLOAD_BATCH_WORKERS = env_int("DOWNLOAD_BATCH_WORKERS", 3, minimum=1)
LOCAL_DOWNLOAD_WORKERS = env_int("LOCAL_DOWNLOAD_WORKERS", 1, minimum=1)
HF_DOWNLOAD_WORKERS = env_int("HF_DOWNLOAD_WORKERS", 1, minimum=1)
CIVITAI_DOWNLOAD_WORKERS = env_int("CIVITAI_DOWNLOAD_WORKERS", 1, minimum=1)
OTHER_DOWNLOAD_WORKERS = env_int("OTHER_DOWNLOAD_WORKERS", 1, minimum=1)
BASE_HOST = urlparse(BASE_URL).netloc.lower()

# --- CONSTANTS ---
USER_AGENT = "ComfyRemoteAgent/0.1"
CHUNK_SIZE = DOWNLOAD_CHUNK_MIB * 1024 * 1024
STALL_TIMEOUT = 45

# --- SETUP ---
COMFY_DIR = None
MODELS_DIR = None
COMFY_DIR_ENV = os.environ.get("COMFY_DIR", "").strip()
DEFAULT_COMFY_DIR = "ComfyUI"

# --- IMPORTS ---
import requests

# Lightning workspaces can use overlay/symlinked filesystems and preloaded user
# site packages. Prefer real venv files and isolate from ambient user packages.
os.environ.setdefault("UV_LINK_MODE", "copy")
os.environ.setdefault("PYTHONNOUSERSITE", "1")

api_session = requests.Session()
api_session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": USER_AGENT
})

download_session = requests.Session()
download_session.headers.update({
    "User-Agent": USER_AGENT
})

_api_lock = threading.Lock()
_keepalive_active = threading.Event()
_progress_lock = threading.Lock()
_progress_event = threading.Event()
_progress_pending = {}
_progress_terminal_tasks = set()
_progress_reporter_started = False

def log(msg, error=False):
    ts = time.strftime("%H:%M:%S")
    prefix = "ERR" if error else "INFO"
    print(f"[{ts}] {prefix} {msg}")

def start_lightning_keepalive():
    if not LIGHTNING_KEEPALIVE:
        return

    def loop():
        log(
            "Lightning keep-awake enabled for active tasks "
            f"({KEEPALIVE_BURST_SECONDS:g}s CPU burst every {KEEPALIVE_INTERVAL_SECONDS:g}s)."
        )
        digest = b"comfy-remote-keepalive"
        while True:
            _keepalive_active.wait()
            while _keepalive_active.is_set():
                end_at = time.time() + KEEPALIVE_BURST_SECONDS
                while time.time() < end_at and _keepalive_active.is_set():
                    digest = hashlib.sha256(digest).digest()
                print(".", end="", flush=True)

                next_burst_at = time.time() + KEEPALIVE_INTERVAL_SECONDS
                while time.time() < next_burst_at and _keepalive_active.is_set():
                    time.sleep(min(1.0, next_burst_at - time.time()))

    threading.Thread(target=loop, name="lightning-keepalive", daemon=True).start()

def set_comfy_dir(path: str):
    global COMFY_DIR, MODELS_DIR
    COMFY_DIR = Path(os.path.expanduser(path)).resolve()
    MODELS_DIR = COMFY_DIR / "models"

def ensure_comfy_dir(task_id=None) -> bool:
    if COMFY_DIR is None:
        msg = "ComfyUI path not set. Restart agent and provide a path."
        if task_id:
            update_progress(task_id, "failed", 0.0, msg)
        else:
            log(msg, error=True)
        return False
    if not COMFY_DIR.exists():
        msg = f"ComfyUI path not found: {COMFY_DIR}"
        if task_id:
            update_progress(task_id, "failed", 0.0, msg)
        else:
            log(msg, error=True)
        return False
    return True

def normalize_asset_relpath(relpath: str) -> Path:
    normalized = str(relpath or "").replace("\\", "/").strip()
    if not normalized:
        raise ValueError("Asset path cannot be empty")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError(f"Unsafe asset path: {relpath}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"Unsafe asset path: {relpath}")
    return Path(*parts)

def get_asset_destination(root_type: str, relpath: str) -> Path:
    root = root_type if root_type in {"input", "workflows"} else "models"
    clean_path = normalize_asset_relpath(relpath)
    if root == "input":
        return COMFY_DIR / "input" / clean_path
    if root == "workflows":
        return COMFY_DIR / "user" / "default" / "workflows" / clean_path
    return MODELS_DIR / clean_path

COMFY_PATH_PREFIXES = {
    "animatediff_models",
    "checkpoints",
    "clip",
    "clip_vision",
    "configs",
    "controlnet",
    "diffusion_models",
    "embeddings",
    "gligen",
    "hypernetworks",
    "insightface",
    "ipadapter",
    "loras",
    "models",
    "photomaker",
    "sams",
    "style_models",
    "text_encoders",
    "unet",
    "upscale_models",
    "vae",
    "vae_approx",
}

COMFY_MODEL_EXTENSIONS = {
    ".bin",
    ".ckpt",
    ".gguf",
    ".onnx",
    ".pth",
    ".pt",
    ".safetensors",
}

def looks_like_comfy_file_reference(value: str) -> bool:
    if "\\" not in value:
        return False
    text = value.strip()
    if not text or "\n" in text or "://" in text or text.lower().startswith(("data:", "http:", "https:")):
        return False
    normalized = text.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized):
        return False
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return False
    first = parts[0].lower()
    suffix = Path(parts[-1]).suffix.lower()
    return first in COMFY_PATH_PREFIXES or suffix in COMFY_MODEL_EXTENSIONS

def normalize_workflow_path_separators(value):
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, item in value.items():
            normalized_item, item_changed = normalize_workflow_path_separators(item)
            result[key] = normalized_item
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            normalized_item, item_changed = normalize_workflow_path_separators(item)
            result.append(normalized_item)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, str) and looks_like_comfy_file_reference(value):
        return value.replace("\\", "/"), True
    return value, False

def postprocess_downloaded_asset(root_type: str, dest_path: Path):
    if root_type != "workflows" or dest_path.suffix.lower() != ".json":
        return
    try:
        raw = dest_path.read_text(encoding="utf-8-sig")
        workflow = json.loads(raw)
        normalized, changed = normalize_workflow_path_separators(workflow)
        if not changed:
            return
        dest_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"Normalized Windows path separators in workflow: {dest_path.name}")
    except Exception as exc:
        log(f"Workflow path normalization skipped for {dest_path.name}: {exc}", error=True)

# --- API WRAPPERS ---

def register_agent():
    log(f"Registering agent with {BASE_URL}...")
    try:
        payload = {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "details": {
                "python": platform.python_version(),
                "cwd": str(os.getcwd()),
                "comfy_dir": str(COMFY_DIR) if COMFY_DIR else None
            }
        }
        resp = api_session.post(f"{BASE_URL}/api/remote/agent/register", json=payload)
        resp.raise_for_status()
        log("✅ Agent registered successfully.")
    except Exception as e:
        log(f"Registration failed: {e}", error=True)
        sys.exit(1)

def fetch_remote_provider_keys():
    global HF_API_KEY, CIVITAI_API_KEY
    try:
        resp = api_session.get(f"{BASE_URL}/api/remote/agent/secrets", timeout=20)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as e:
        log(f"Could not fetch provider keys from manager: {e}", error=True)
        return

    hf_key = (data.get("huggingface_api_key") or "").strip()
    civitai_key = (data.get("civitai_api_key") or "").strip()

    if not HF_API_KEY and hf_key:
        HF_API_KEY = hf_key
        log("Received Hugging Face key from manager.")
    if not CIVITAI_API_KEY and civitai_key:
        CIVITAI_API_KEY = civitai_key
        log("Received Civitai key from manager.")

def get_next_task():
    try:
        resp = api_session.get(f"{BASE_URL}/api/remote/tasks/next")
        if resp.status_code == 200:
            return resp.json() # Returns Task or None
        return None
    except Exception as e:
        log(f"Polling error: {e}", error=True)
        return None

def post_progress_payload(payload):
    if payload.get("status") == "running":
        with _progress_lock:
            if payload.get("task_id") in _progress_terminal_tasks:
                return
    try:
        with _api_lock:
            api_session.post(
                f"{BASE_URL}/api/remote/tasks/progress",
                json=payload,
                timeout=PROGRESS_POST_TIMEOUT_SECONDS,
            )
    except Exception:
        pass

def start_progress_reporter():
    global _progress_reporter_started
    if _progress_reporter_started:
        return
    _progress_reporter_started = True

    def loop():
        while True:
            _progress_event.wait(timeout=PROGRESS_UPDATE_INTERVAL_SECONDS)
            _progress_event.clear()
            with _progress_lock:
                pending = list(_progress_pending.values())
                _progress_pending.clear()
            for payload in pending:
                post_progress_payload(payload)

    threading.Thread(target=loop, name="progress-reporter", daemon=True).start()

def update_progress(task_id, status, progress=None, message=None, error=None, meta=None):
    payload = {"task_id": task_id, "status": status}
    if progress is not None: payload["progress"] = progress
    if message: payload["message"] = message
    if error: payload["error"] = error
    if meta: payload["meta"] = meta

    if status == "running":
        start_progress_reporter()
        with _progress_lock:
            if task_id in _progress_terminal_tasks:
                return
            _progress_pending[task_id] = payload
        _progress_event.set()
        return

    if status in {"completed", "failed", "cancelled"}:
        with _progress_lock:
            _progress_terminal_tasks.add(task_id)
            _progress_pending.pop(task_id, None)

    post_progress_payload(payload)

def is_task_cancelled(task_id: str) -> bool:
    try:
        with _api_lock:
            resp = api_session.get(f"{BASE_URL}/api/remote/tasks/{task_id}", timeout=10)
        if resp.status_code != 200:
            return False
        data = resp.json() or {}
        return data.get("status") == "cancelled"
    except Exception:
        return False

def make_cancel_checker(task_id: str, interval_seconds: float = 2.0):
    cancel_event = threading.Event()
    lock = threading.Lock()
    last_check = [0.0]

    def should_cancel():
        if cancel_event.is_set():
            return True

        now = time.time()
        with lock:
            if now - last_check[0] < interval_seconds:
                return cancel_event.is_set()
            last_check[0] = now

        if is_task_cancelled(task_id):
            cancel_event.set()
            return True
        return False

    return should_cancel, cancel_event

# --- HELPERS ---

def get_provider_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "unknown"
    if BASE_HOST and host == BASE_HOST:
        # Treat any local base host URLs as local provider
        return "local"
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

def get_venv_script(name: str) -> Path:
    venv_path = COMFY_DIR / ".venv"
    if platform.system().lower().startswith("win"):
        return venv_path / "Scripts" / f"{name}.exe"
    return venv_path / "bin" / name

def subprocess_env():
    env = os.environ.copy()
    if COMFY_DIR:
        venv_path = COMFY_DIR / ".venv"
        script_dir = venv_path / ("Scripts" if platform.system().lower().startswith("win") else "bin")
        if script_dir.exists():
            env["VIRTUAL_ENV"] = str(venv_path)
            env["PATH"] = str(script_dir) + os.pathsep + env.get("PATH", "")
    return env

def run_cmd(cmd, cwd=None):
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=subprocess_env(),
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

def package_to_import_name(spec: str) -> str:
    # Strip version/extra markers from package spec; fallback dash->underscore.
    base = re.split(r"[<>=!~\[\]]", spec, maxsplit=1)[0].strip()
    if not base:
        return spec.replace("-", "_")
    return base.replace("-", "_")

def install_optional_package(venv_python: Path, package: str) -> bool:
    attempts = [
        [str(venv_python), "-m", "pip", "install", "-U", package],
    ]
    package_name = package_to_import_name(package).lower()
    if package_name == "flash_attn":
        attempts.append([str(venv_python), "-m", "pip", "install", "-U", package, "--no-build-isolation"])

    last_err = ""
    for cmd in attempts:
        rc, _, err = run_cmd(cmd, cwd=COMFY_DIR)
        if rc == 0:
            return True
        last_err = err

    log(f"Optional package install failed for {package}: {last_err}", error=True)
    return False

# --- TASK HANDLERS ---

def handle_git_clone(task):
    payload = task.get('payload', {})
    repo_url = payload.get('repo_url', "https://github.com/comfyanonymous/ComfyUI.git")
    dest = payload.get('dest_path', str(COMFY_DIR) if COMFY_DIR else "")
    if not dest:
        update_progress(task['id'], "failed", 0.0, "ComfyUI path not set. Restart agent and provide a path.")
        return
    dest_path = Path(dest)

    update_progress(task['id'], "running", 0.0, f"Cloning {repo_url}...")
    
    if dest_path.exists() and (dest_path / ".git").exists():
        log("ComfyUI already exists. Skipping clone.")
        update_progress(task['id'], "completed", 1.0, "Already exists")
        return

    try:
        log(f"Cloning to {dest_path}...")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
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
    if not ensure_comfy_dir(task['id']):
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

    if not ensure_comfy_dir(task['id']):
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
    if not ensure_comfy_dir(task['id']):
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
    if rc != 0:
        update_progress(task['id'], "failed", 0.0, "Requirements install failed", error=err)
        return

    manager_requirements = COMFY_DIR / "manager_requirements.txt"
    if manager_requirements.exists():
        update_progress(task['id'], "running", 0.55, "Installing native ComfyUI Manager requirements...")
        manager_cmd = [str(venv_python), "-m", "pip", "install", "-r", "manager_requirements.txt"]
        rc, _, err = run_cmd(manager_cmd, cwd=COMFY_DIR)
        if rc != 0:
            update_progress(task['id'], "failed", 0.0, "Native manager requirements install failed", error=err)
            return
    else:
        log("manager_requirements.txt not found; skipping native manager dependencies.", error=True)

    if EXTRA_PIP_PACKAGES:
        extras_label = " ".join(EXTRA_PIP_PACKAGES)
        update_progress(task['id'], "running", 0.75, f"Installing extra dependencies: {extras_label}")
        extra_cmd = [str(venv_python), "-m", "pip", "install", "-U", *EXTRA_PIP_PACKAGES]
        rc, _, err = run_cmd(extra_cmd, cwd=COMFY_DIR)
        if rc != 0:
            update_progress(task['id'], "failed", 0.0, "Extra dependency install failed", error=err)
            return

        imports_to_verify = [package_to_import_name(p) for p in EXTRA_PIP_PACKAGES]
        update_progress(task['id'], "running", 0.9, f"Verifying imports: {' '.join(imports_to_verify)}")
        for mod in imports_to_verify:
            verify_cmd = [str(venv_python), "-c", f"import {mod}"]
            rc, _, err = run_cmd(verify_cmd, cwd=COMFY_DIR)
            if rc != 0:
                update_progress(task['id'], "failed", 0.0, f"Import check failed: {mod}", error=err)
                return

    if OPTIONAL_PIP_PACKAGES:
        optional_label = " ".join(OPTIONAL_PIP_PACKAGES)
        update_progress(task['id'], "running", 0.93, f"Attempting optional acceleration dependencies: {optional_label}")
        for package in OPTIONAL_PIP_PACKAGES:
            install_optional_package(venv_python, package)

    if not apply_dependency_compatibility_fixes(task['id'], 0.95):
        return

    update_progress(task['id'], "completed", 1.0, "Requirements installed; launch ComfyUI with --enable-manager")

def handle_install_manager(task):
    if not ensure_comfy_dir(task['id']):
        return

    venv_python = get_venv_python()
    if not venv_python.exists():
        update_progress(task['id'], "failed", 0.0, "Venv not found. Create venv first.")
        return

    ok, err = ensure_pip(task['id'])
    if not ok:
        update_progress(task['id'], "failed", 0.0, "pip is missing in venv", error=err)
        return

    manager_requirements = COMFY_DIR / "manager_requirements.txt"
    if not manager_requirements.exists():
        update_progress(
            task['id'],
            "failed",
            0.0,
            "manager_requirements.txt not found. Update ComfyUI before enabling native manager.",
        )
        return

    update_progress(task['id'], "running", 0.0, "Installing native ComfyUI Manager requirements...")
    rc, _, err = run_cmd([str(venv_python), "-m", "pip", "install", "-r", "manager_requirements.txt"], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task['id'], "failed", 0.0, "Native manager requirements install failed", error=err)
        return

    rc, _, err = run_cmd([str(venv_python), "-c", "import cm_cli"], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task['id'], "failed", 0.0, "Native manager installed but cm_cli is not importable", error=err)
        return

    legacy_dirs = [
        COMFY_DIR / "custom_nodes" / "comfyui-manager",
        COMFY_DIR / "custom_nodes" / "ComfyUI-Manager",
    ]
    legacy_found = [str(path) for path in legacy_dirs if path.exists()]
    if legacy_found:
        msg = (
            "Native manager dependencies installed. Legacy custom-node manager is still present; "
            "remove or disable it to avoid duplicate manager behavior. "
            f"Launch ComfyUI with --enable-manager. Legacy paths: {', '.join(legacy_found)}"
        )
    else:
        msg = "Native manager enabled. Launch ComfyUI with --enable-manager."

    update_progress(task['id'], "completed", 1.0, msg)

def ensure_official_manager_cli(task_id: str, progress: float = 0.0) -> bool:
    """Ensure Comfy CLI and cm_cli are available for official registry node installs."""
    venv_python = get_venv_python()
    manager_requirements = COMFY_DIR / "manager_requirements.txt"
    if not manager_requirements.exists():
        update_progress(task_id, "failed", progress, "manager_requirements.txt not found; cannot use official Comfy node install.")
        return False

    rc, _, err = run_cmd([str(venv_python), "-m", "pip", "install", "-U", "comfy-cli"], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task_id, "failed", progress, "Failed to install comfy-cli", error=err)
        return False

    rc, _, err = run_cmd([str(venv_python), "-m", "pip", "install", "-r", "manager_requirements.txt"], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task_id, "failed", progress, "Failed to install manager_requirements.txt", error=err)
        return False

    rc, _, err = run_cmd([str(venv_python), "-c", "import cm_cli"], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task_id, "failed", progress, "cm_cli is not importable after manager requirements install", error=err)
        return False

    comfy_exe = get_venv_script("comfy")
    if comfy_exe.exists():
        run_cmd([str(comfy_exe), "--skip-prompt", "--workspace", str(COMFY_DIR), "manager", "enable-gui"], cwd=COMFY_DIR)

    return True

def safe_custom_node_dir_name(value: str) -> str:
    value = value.rstrip("/").split("/")[-1]
    if value.endswith(".git"):
        value = value[:-4]
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return value or "custom-node"

def custom_node_present(custom_nodes_dir: Path, *values: str) -> bool:
    for value in values:
        if not value:
            continue
        if (custom_nodes_dir / safe_custom_node_dir_name(value)).exists():
            return True
    return False

def install_custom_node_from_git(task_id, name, git_url, custom_nodes_dir, venv_python, progress_base, progress_done):
    if not git_url.startswith(("http://", "https://", "git@")):
        update_progress(task_id, "failed", progress_base, f"No usable Git URL for {name}")
        return False

    dest = custom_nodes_dir / safe_custom_node_dir_name(git_url)
    if dest.exists():
        update_progress(task_id, "running", progress_base, f"Custom node already present: {name}")
    else:
        rc, _, git_err = run_cmd(["git", "clone", git_url, str(dest)], cwd=custom_nodes_dir)
        if rc != 0:
            update_progress(task_id, "failed", progress_base, f"Git clone failed: {name}", error=git_err)
            return False

    requirements = dest / "requirements.txt"
    if requirements.exists():
        rc, _, req_err = run_cmd([str(venv_python), "-m", "pip", "install", "-r", str(requirements)], cwd=COMFY_DIR)
        if rc != 0:
            update_progress(task_id, "failed", progress_base, f"Requirements install failed: {name}", error=req_err)
            return False

    install_py = dest / "install.py"
    if install_py.exists():
        rc, _, install_err = run_cmd([str(venv_python), str(install_py)], cwd=dest)
        if rc != 0:
            update_progress(task_id, "failed", progress_base, f"Custom node install.py failed: {name}", error=install_err)
            return False

    update_progress(task_id, "running", progress_done, f"Installed custom node: {name}")
    return True

def apply_dependency_compatibility_fixes(task_id, progress=0.95):
    """Apply narrow dependency remediations for known Comfy/custom-node import traps."""
    if not TRANSFORMERS_COMPAT_PIN:
        return True

    venv_python = get_venv_python()
    if not venv_python.exists():
        update_progress(task_id, "failed", progress, "Venv not found for dependency compatibility check.")
        return False

    check_code = r"""
try:
    from transformers.utils.import_utils import is_flash_attn_2_available
except Exception:
    raise SystemExit(0)
try:
    is_flash_attn_2_available()
except KeyError as exc:
    if str(exc).strip("'\"") == "flash_attn":
        raise SystemExit(42)
    raise
"""
    rc, _, err = run_cmd([str(venv_python), "-c", check_code], cwd=COMFY_DIR)
    if rc == 0:
        return True
    if rc != 42:
        log(f"Transformers compatibility probe failed without known flash_attn bug: {err}", error=True)
        return True

    update_progress(task_id, "running", progress, f"Applying compatibility pin: {TRANSFORMERS_COMPAT_PIN}")
    rc, _, install_err = run_cmd([str(venv_python), "-m", "pip", "install", TRANSFORMERS_COMPAT_PIN], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task_id, "failed", progress, f"Failed to apply compatibility pin: {TRANSFORMERS_COMPAT_PIN}", error=install_err)
        return False

    rc, _, verify_err = run_cmd([str(venv_python), "-c", check_code], cwd=COMFY_DIR)
    if rc != 0:
        update_progress(task_id, "failed", progress, "Transformers compatibility check still fails after pin.", error=verify_err)
        return False

    log(f"Applied compatibility pin: {TRANSFORMERS_COMPAT_PIN}")
    return True

def handle_install_custom_nodes(task):
    payload = task.get("payload", {})
    nodes = payload.get("nodes") or []
    if not nodes:
        update_progress(task["id"], "completed", 1.0, "No custom nodes to install")
        return

    if not ensure_comfy_dir(task["id"]):
        return

    venv_python = get_venv_python()
    if not venv_python.exists():
        update_progress(task["id"], "failed", 0.0, "Venv not found. Create venv first.")
        return

    ok, err = ensure_pip(task["id"])
    if not ok:
        update_progress(task["id"], "failed", 0.0, "pip is missing in venv", error=err)
        return

    total = len(nodes)
    custom_nodes_dir = COMFY_DIR / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)

    for idx, node in enumerate(nodes, start=1):
        install_type = (node.get("install_type") or "registry").lower()
        node_id = (node.get("node_id") or "").strip()
        name = node.get("name") or node_id
        repo = (node.get("repository") or "").strip()
        progress_base = (idx - 1) / total
        progress_done = idx / total

        if not node_id:
            continue

        update_progress(task["id"], "running", progress_base, f"Installing custom node {idx}/{total}: {name}")

        if install_type == "registry":
            if not ensure_official_manager_cli(task["id"], progress_base):
                return

            comfy_exe = get_venv_script("comfy")
            if comfy_exe.exists():
                rc, out, cli_err = run_cmd([str(comfy_exe), "--skip-prompt", "--workspace", str(COMFY_DIR), "node", "install", node_id], cwd=COMFY_DIR)
            else:
                comfy_path = shutil.which("comfy")
                if comfy_path:
                    rc, out, cli_err = run_cmd([comfy_path, "--skip-prompt", "--workspace", str(COMFY_DIR), "node", "install", node_id], cwd=COMFY_DIR)
                else:
                    rc, out, cli_err = 127, "", "comfy command not found after installing comfy-cli"
            if rc == 0:
                if not repo or custom_node_present(custom_nodes_dir, repo, name, node_id):
                    update_progress(task["id"], "running", progress_done, f"Installed custom node: {name}")
                    continue
                log(f"Registry install reported success but no custom_nodes directory was found for {name}; falling back to Git clone.")
            elif not repo:
                cli_output = f"{out}\n{cli_err}".strip()
                update_progress(task["id"], "failed", progress_base, f"Official registry install failed: {name}", error=cli_output)
                return

            cli_output = f"{out}\n{cli_err}".strip()
            if cli_output:
                log(f"Official registry install did not complete for {name}; falling back to Git clone. Output: {cli_output}")

            if install_custom_node_from_git(task["id"], name, repo, custom_nodes_dir, venv_python, progress_base, progress_done):
                continue
            return

        git_url = repo or node_id
        if not install_custom_node_from_git(task["id"], name, git_url, custom_nodes_dir, venv_python, progress_base, progress_done):
            return

    if not apply_dependency_compatibility_fixes(task["id"], 0.98):
        return

    update_progress(task["id"], "completed", 1.0, f"Installed {total} custom node pack(s). Restart ComfyUI to load them.")

def parse_content_range_total(value):
    if not value:
        return 0
    match = re.search(r"/(\d+)$", value.strip())
    if not match:
        return 0
    return int(match.group(1))

def probe_range_download(url, headers, should_cancel=None):
    if DOWNLOAD_SEGMENTS <= 1:
        return 0
    if should_cancel and should_cancel():
        return 0

    probe_headers = dict(headers or {})
    probe_headers["Range"] = "bytes=0-0"
    try:
        with requests.get(url, headers=probe_headers, stream=True, timeout=STALL_TIMEOUT) as r:
            if r.status_code != 206:
                log(f"Range probe returned HTTP {r.status_code}; using single-stream fallback.")
                return 0
            total_size = parse_content_range_total(r.headers.get("content-range"))
            if total_size < DOWNLOAD_SEGMENT_MIN_MIB * 1024 * 1024:
                log(f"Range supported, but file is below {DOWNLOAD_SEGMENT_MIN_MIB} MiB segment threshold.")
                return 0
            return total_size
    except Exception as e:
        log(f"Range probe failed; using single-stream fallback: {e}")
        return 0

def segmented_range_download(
    url,
    dest_path,
    task_id,
    total_size,
    extra_headers=None,
    should_cancel=None,
    progress_callback=None,
):
    min_segment_bytes = DOWNLOAD_SEGMENT_MIN_MIB * 1024 * 1024
    segment_count = min(DOWNLOAD_SEGMENTS, max(1, (total_size + min_segment_bytes - 1) // min_segment_bytes))
    if segment_count <= 1:
        return False, "segmented download not useful"

    headers_base = dict(extra_headers or {})
    segment_size = total_size // segment_count
    segments = []
    for index in range(segment_count):
        start = index * segment_size
        end = total_size - 1 if index == segment_count - 1 else ((index + 1) * segment_size) - 1
        part_path = Path(f"{dest_path}.seg{index}")
        segments.append((index, start, end, part_path))

    downloaded_by_segment = [0] * segment_count
    lock = threading.Lock()
    last_progress_ts = [0.0]

    for index, start, end, part_path in segments:
        expected = end - start + 1
        if part_path.exists():
            current = part_path.stat().st_size
            if current > expected:
                part_path.unlink()
                current = 0
            downloaded_by_segment[index] = current

    def report_progress(force=False):
        now = time.time()
        with lock:
            if not force and now - last_progress_ts[0] <= PROGRESS_UPDATE_INTERVAL_SECONDS:
                return
            last_progress_ts[0] = now
            downloaded = sum(downloaded_by_segment)
        pct = downloaded / total_size if total_size else 0
        if progress_callback:
            progress_callback(downloaded, total_size, pct)
        else:
            update_progress(task_id, "running", pct, f"Downloading: {int(pct * 100)}%")

    def download_segment(index, start, end, part_path):
        expected = end - start + 1
        existing = part_path.stat().st_size if part_path.exists() else 0
        if existing == expected:
            report_progress(force=True)
            return
        if existing > expected:
            part_path.unlink()
            existing = 0

        headers = dict(headers_base)
        headers["Range"] = f"bytes={start + existing}-{end}"
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        mode = "ab" if existing else "wb"

        with session.get(url, headers=headers, stream=True, timeout=STALL_TIMEOUT) as r:
            if r.status_code != 206:
                raise RuntimeError(f"Range request returned HTTP {r.status_code}")
            with open(part_path, mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if should_cancel and should_cancel():
                        raise RuntimeError("cancelled")
                    if not chunk:
                        continue
                    f.write(chunk)
                    with lock:
                        downloaded_by_segment[index] += len(chunk)
                    report_progress()

        if part_path.stat().st_size != expected:
            raise RuntimeError(f"segment {index + 1}/{segment_count} incomplete")

    log(f"Downloading {dest_path.name} with {segment_count} parallel byte ranges")
    try:
        with ThreadPoolExecutor(max_workers=segment_count) as executor:
            futures = [
                executor.submit(download_segment, index, start, end, part_path)
                for index, start, end, part_path in segments
            ]
            for future in futures:
                future.result()

        if should_cancel and should_cancel():
            return False, "cancelled"

        with open(dest_path, "wb") as out:
            for _, _, _, part_path in segments:
                with open(part_path, "rb") as part:
                    shutil.copyfileobj(part, out, length=CHUNK_SIZE)

        for _, _, _, part_path in segments:
            try:
                part_path.unlink()
            except OSError:
                pass
        report_progress(force=True)
        return True, None
    except Exception as e:
        if (should_cancel and should_cancel()) or str(e) == "cancelled":
            return False, "cancelled"
        return False, str(e)

def download_from_source(
    url,
    dest_path,
    task_id,
    existing_size=0,
    extra_headers=None,
    session=None,
    should_cancel=None,
    progress_callback=None,
):
    sess = session or download_session
    attempt = 0
    while attempt < DOWNLOAD_MAX_RETRIES:
        if should_cancel and should_cancel():
            return False, "cancelled"
        attempt += 1
        headers = {}
        if extra_headers:
            headers.update(extra_headers)
        mode = 'wb'
        attempt_existing = existing_size
        if attempt_existing > 0:
            headers['Range'] = f'bytes={attempt_existing}-'
            mode = 'ab'

        try:
            range_total_size = 0
            if attempt_existing == 0:
                range_total_size = probe_range_download(url, headers, should_cancel=should_cancel)

                if range_total_size:
                    ok, err = segmented_range_download(
                        url,
                        dest_path,
                        task_id,
                        range_total_size,
                        extra_headers=headers,
                        should_cancel=should_cancel,
                        progress_callback=progress_callback,
                    )
                    if ok or err == "cancelled":
                        return ok, err
                    log(f"Segmented download unavailable, falling back to single stream: {err}")

            with sess.get(url, headers=headers, stream=True, timeout=STALL_TIMEOUT) as r:
                # Retry transient upstream server failures
                if r.status_code >= 500:
                    body_preview = ""
                    try:
                        body_preview = (r.text or "").strip()
                    except Exception:
                        body_preview = ""
                    if attempt < DOWNLOAD_MAX_RETRIES:
                        wait_s = DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt
                        log(f"Transient HTTP {r.status_code} from source, retrying in {wait_s:.1f}s (attempt {attempt}/{DOWNLOAD_MAX_RETRIES})")
                        time.sleep(wait_s)
                        continue
                    snippet = f" Body: {body_preview[:240]}" if body_preview else ""
                    return False, f"Upstream HTTP {r.status_code} from source after {DOWNLOAD_MAX_RETRIES} attempts.{snippet}"

                r.raise_for_status()

                # If server ignored Range, restart file write from zero.
                if attempt_existing > 0 and r.status_code == 200:
                    mode = 'wb'
                    attempt_existing = 0

                total_size = int(r.headers.get('content-length', 0)) + attempt_existing
                downloaded = attempt_existing

                log(
                    f"Downloading to {dest_path.name} (Resuming from {attempt_existing})"
                    if attempt_existing else f"Downloading {dest_path.name}"
                )

                with open(dest_path, mode) as f:
                    last_update = time.time()
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if should_cancel and should_cancel():
                            return False, "cancelled"
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                            # Throttle updates to keep the UI responsive without hammering the controller.
                            now = time.time()
                            if now - last_update > PROGRESS_UPDATE_INTERVAL_SECONDS:
                                pct = downloaded / total_size if total_size else 0
                                if progress_callback:
                                    progress_callback(downloaded, total_size, pct)
                                else:
                                    update_progress(task_id, "running", pct, f"Downloading: {int(pct*100)}%")
                                last_update = now

                if progress_callback:
                    pct = downloaded / total_size if total_size else 1.0
                    progress_callback(downloaded, total_size, pct)

                return True, None
        except Exception as e:
            if should_cancel and should_cancel():
                return False, "cancelled"
            if attempt < DOWNLOAD_MAX_RETRIES:
                wait_s = DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt
                log(f"Download error, retrying in {wait_s:.1f}s (attempt {attempt}/{DOWNLOAD_MAX_RETRIES}): {e}", error=True)
                time.sleep(wait_s)
                continue
            return False, str(e)

    return False, "Download retries exhausted."

def handle_download(task):
    payload = task.get('payload', {})
    file_hash = payload.get('hash')
    relpath = payload.get('relpath')
    root_type = payload.get('root_type', 'models')
    
    if not ensure_comfy_dir(task['id']):
        return

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
        
    root_type = resolution.get('root_type') or root_type
    try:
        final_dest = get_asset_destination(root_type, resolution['relpath'])
    except ValueError as e:
        update_progress(task['id'], "failed", 0.0, str(e), error=str(e))
        return
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if exists (Simple check, not verifying hash in this iteration)
    if final_dest.exists():
        log(f"File {final_dest.name} already exists. Skipping.")
        postprocess_downloaded_asset(root_type, final_dest)
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
        should_cancel, _ = make_cancel_checker(task['id'])
        ok, err = download_from_source(
            url,
            temp_dest,
            task['id'],
            current_size,
            extra_headers=headers,
            should_cancel=should_cancel,
        )
        
        if ok:
            temp_dest.rename(final_dest)
            postprocess_downloaded_asset(root_type, final_dest)
            success = True
            break
        elif err == "cancelled":
            update_progress(task['id'], "cancelled", 0.0, "Cancelled by user.")
            return
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

    if not ensure_comfy_dir(task['id']):
        return

    total_items = len(items)
    items_status = {}
    normalized_items = []

    for i, item in enumerate(items):
        relpath = item.get('relpath')
        url = item.get('url')
        size_bytes = item.get('size_bytes') or item.get('size')
        provider = item.get('provider') or (get_provider_from_url(url) if url else "unknown")
        root_type = item.get('root_type') or "models"
        key = f"{root_type}:{relpath}" if relpath else (url or f"item_{i+1}")
        items_status[key] = "pending"
        normalized_items.append({
            "key": key,
            "relpath": relpath,
            "root_type": root_type,
            "url": url,
            "size_bytes": size_bytes,
            "provider": provider,
        })

    update_progress(
        task['id'],
        "running",
        0.0,
        f"Starting batch download of {total_items} items...",
        meta={"items_status": items_status, "items_total": total_items, "items_done": 0}
    )

    lock = threading.Lock()
    done_count = [0]
    active_item_progress = {}
    active_item_speed = {}
    item_progress_samples = {}
    should_cancel, cancel_event = make_cancel_checker(task['id'])

    def update_item(key, status, message=None, done_delta=0):
        with lock:
            items_status[key] = status
            if status in ("completed", "failed", "skipped"):
                active_item_progress.pop(key, None)
                active_item_speed.pop(key, None)
                item_progress_samples.pop(key, None)
            done_count[0] += done_delta
            done = done_count[0]
        progress = done / total_items if total_items else 1.0
        update_progress(
            task['id'],
            "running",
            progress,
            message,
            meta={"items_status": {key: status}, "items_done": done}
        )

    def update_item_download_progress(key, downloaded, total_size, pct, relpath):
        now = time.time()
        with lock:
            previous = item_progress_samples.get(key)
            bytes_per_second = active_item_speed.get(key)
            if previous:
                prev_downloaded, prev_time = previous
                elapsed = now - prev_time
                byte_delta = downloaded - prev_downloaded
                if elapsed > 0 and byte_delta >= 0:
                    instant_bps = byte_delta / elapsed
                    bytes_per_second = (
                        instant_bps
                        if bytes_per_second is None
                        else (bytes_per_second * 0.65) + (instant_bps * 0.35)
                    )
                    active_item_speed[key] = bytes_per_second
            item_progress_samples[key] = (downloaded, now)
            active_item_progress[key] = pct
            done = done_count[0]
            active_sum = sum(active_item_progress.values())
        progress = min(1.0, (done + active_sum) / total_items) if total_items else 1.0
        percent_label = f"{int(pct * 100)}%" if total_size else f"{downloaded} bytes"
        update_progress(
            task['id'],
            "running",
            progress,
            f"Downloading: {relpath} ({percent_label})",
            meta={
                "items_status": {key: "downloading"},
                "items_progress": {
                    key: {
                        "downloaded": downloaded,
                        "total": total_size,
                        "pct": pct,
                        "bytes_per_second": bytes_per_second,
                    }
                },
                "items_done": done,
            }
        )

    def sort_items(items_list, ascending=True):
        def size_key(item):
            size = item.get("size_bytes")
            if size is None:
                return (1, 0)  # Unknown sizes go last
            return (0, size if ascending else -size)
        return sorted(items_list, key=size_key)

    queues = {
        "local": [],
        "huggingface": [],
        "civitai": [],
        "other": [],
    }

    for item in normalized_items:
        provider = item["provider"]
        if provider not in queues:
            provider = "other"
        queues[provider].append(item)

    queues["local"] = sort_items(queues["local"], ascending=True)
    queues["huggingface"] = sort_items(queues["huggingface"], ascending=False)
    queues["civitai"] = sort_items(queues["civitai"], ascending=False)
    queues["other"] = sort_items(queues["other"], ascending=False)

    def worker(provider, queue_items):
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})

        for item in queue_items:
            if should_cancel():
                break
            relpath = item.get('relpath')
            root_type = item.get('root_type') or "models"
            url = item.get('url')
            item_key = item.get('key')

            if not relpath or not url:
                update_item(item_key, "skipped", f"Skipping {item_key} (missing data)", done_delta=1)
                continue

            if provider == "huggingface" and not HF_API_KEY:
                log(f"Skipping Hugging Face URL (no HF key provided): {relpath}")
                update_item(item_key, "skipped", f"Skipped HF (no key): {relpath}", done_delta=1)
                continue
            if provider == "civitai" and not CIVITAI_API_KEY:
                log(f"Skipping Civitai URL (no Civitai key provided): {relpath}")
                update_item(item_key, "skipped", f"Skipped Civitai (no key): {relpath}", done_delta=1)
                continue

            try:
                dest_path = get_asset_destination(root_type, relpath)
            except ValueError as e:
                log(str(e), error=True)
                update_item(item_key, "failed", str(e), done_delta=1)
                continue
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            if dest_path.exists():
                log(f"{relpath} already exists. Skipping.")
                postprocess_downloaded_asset(root_type, dest_path)
                update_item(item_key, "skipped", f"Skipping existing: {relpath}", done_delta=1)
                continue

            update_item(item_key, "downloading", f"Downloading: {relpath}")

            temp_dest = dest_path.with_suffix(dest_path.suffix + ".part")
            current_size = temp_dest.stat().st_size if temp_dest.exists() else 0

            headers = auth_headers_for_source(provider, url)
            ok, err = download_from_source(
                url,
                temp_dest,
                task['id'],
                current_size,
                extra_headers=headers,
                session=session,
                should_cancel=should_cancel,
                progress_callback=lambda downloaded, total_size, pct, key=item_key, path=relpath: update_item_download_progress(
                    key,
                    downloaded,
                    total_size,
                    pct,
                    path,
                ),
            )

            if ok:
                temp_dest.rename(dest_path)
                postprocess_downloaded_asset(root_type, dest_path)
                log(f"Successfully downloaded {relpath}")
                update_item(item_key, "completed", f"Completed: {relpath}", done_delta=1)
            elif err == "cancelled":
                cancel_event.set()
                break
            else:
                log(f"Failed to download {relpath}: {err}", error=True)
                update_item(item_key, "failed", f"Failed: {relpath}", done_delta=1)

    provider_worker_limits = {
        "local": LOCAL_DOWNLOAD_WORKERS,
        "huggingface": HF_DOWNLOAD_WORKERS,
        "civitai": CIVITAI_DOWNLOAD_WORKERS,
        "other": OTHER_DOWNLOAD_WORKERS,
    }

    def split_provider_queue(provider, items):
        if not items:
            return []
        worker_limit = provider_worker_limits.get(provider, 1)
        worker_count = min(worker_limit, len(items))
        return [
            (provider, items[index::worker_count])
            for index in range(worker_count)
            if items[index::worker_count]
        ]

    active_queues = []
    for provider, items in queues.items():
        active_queues.extend(split_provider_queue(provider, items))
    if active_queues:
        batch_workers = min(DOWNLOAD_BATCH_WORKERS, len(active_queues))
        with ThreadPoolExecutor(max_workers=batch_workers) as executor:
            futures = [executor.submit(worker, provider, items) for provider, items in active_queues]
            for f in futures:
                f.result()

    if should_cancel():
        done = done_count[0]
        progress = done / total_items if total_items else 1.0
        update_progress(task['id'], "cancelled", progress, f"Cancelled by user. Processed {done}/{total_items} items.")
        return

    failed = sum(1 for s in items_status.values() if s == "failed")
    skipped = sum(1 for s in items_status.values() if s == "skipped")
    completed = sum(1 for s in items_status.values() if s == "completed")
    msg = f"Batch download finished. Completed {completed}/{total_items}"
    if skipped:
        msg += f", skipped {skipped}"
    if failed:
        msg += f", failed {failed}"

    update_progress(task['id'], "completed", 1.0, msg)

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

    # Configure ComfyUI directory
    comfy_path = COMFY_DIR_ENV
    if not comfy_path:
        try:
            comfy_path = input(f"Enter ComfyUI path (blank for default: {DEFAULT_COMFY_DIR}): ").strip()
        except KeyboardInterrupt:
            return

    if not comfy_path:
        comfy_path = DEFAULT_COMFY_DIR
        print(f"Using default ComfyUI path: {comfy_path}")

    expanded = Path(os.path.expanduser(comfy_path)).resolve()
    if not expanded.exists():
        if CREATE_COMFY_DIR:
            print(f"Creating ComfyUI path: {expanded}")
        else:
            try:
                resp = input(f"Path does not exist. Create it? [y/N]: ").strip().lower()
            except KeyboardInterrupt:
                return
            if resp != "y":
                print("ComfyUI path not created. Exiting.")
                return
        expanded.mkdir(parents=True, exist_ok=True)

    set_comfy_dir(str(expanded))

    start_lightning_keepalive()
    register_agent()
    fetch_remote_provider_keys()

    if PROMPT_OPTIONAL_KEYS:
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
    
    log("Waiting for tasks (Ctrl+C to stop)...")
    
    while True:
        try:
            # 1. Heartbeat
            api_session.post(f"{BASE_URL}/api/remote/agent/heartbeat")
            
            # 2. Get Task (with long-polling timeout on server side)
            task = get_next_task()
            if task:
                log(f"Received Task: {task['type']} ({task['id']})")

                _keepalive_active.set()
                try:
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
                    elif task['type'] in ('INSTALL_COMFYUI_MANAGER', 'ENABLE_NATIVE_MANAGER'):
                        handle_install_manager(task)
                    elif task['type'] == 'INSTALL_CUSTOM_NODES':
                        handle_install_custom_nodes(task)
                    else:
                        log(f"Unknown task type: {task['type']}")
                        update_progress(task['id'], "failed", error="Unknown task type")
                finally:
                    _keepalive_active.clear()
            
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
