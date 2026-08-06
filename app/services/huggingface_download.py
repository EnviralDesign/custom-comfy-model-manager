"""Native Hugging Face Hub downloads backed by hf-xet."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from app.config import get_settings

# hf-xet reads these settings when the Hub client is initialized. Respect an
# explicit user override while defaulting this app to its high-throughput path.
os.environ.setdefault(
    "HF_XET_HIGH_PERFORMANCE",
    "1" if get_settings().huggingface_xet_high_performance else "0",
)
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "45")

from huggingface_hub import hf_hub_download
from huggingface_hub.utils import tqdm as hf_tqdm


ProgressCallback = Callable[[int, int | None], None]
CancelCallback = Callable[[], bool]


class HuggingFaceDownloadCancelled(RuntimeError):
    """Raised when an in-progress native Hub download is cancelled."""


@dataclass(frozen=True)
class HuggingFaceFileRef:
    repo_id: str
    filename: str
    revision: str = "main"
    repo_type: str | None = None


def parse_huggingface_file_url(url: str) -> HuggingFaceFileRef | None:
    """Parse a Hugging Face file URL into the fields used by hf_hub_download."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = (parsed.hostname or "").lower()
    if host not in {"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"}:
        return None

    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    repo_type = None
    if parts and parts[0] in {"datasets", "spaces"}:
        repo_type = "dataset" if parts[0] == "datasets" else "space"
        parts = parts[1:]

    if len(parts) < 5 or parts[2] not in {"resolve", "blob"}:
        return None

    repo_id = f"{parts[0]}/{parts[1]}"
    tail = parts[3:]

    # Common refs such as refs/pr/123 are normally URL-encoded, but accepting
    # their unencoded form makes URLs produced by older manager versions work.
    if len(tail) >= 4 and tail[0] == "refs" and tail[1] in {"pr", "convert"}:
        revision = "/".join(tail[:3])
        filename_parts = tail[3:]
    else:
        revision = tail[0]
        filename_parts = tail[1:]

    if not revision or not filename_parts or any(part in {".", ".."} for part in filename_parts):
        return None

    return HuggingFaceFileRef(
        repo_id=repo_id,
        filename="/".join(filename_parts),
        revision=revision,
        repo_type=repo_type,
    )


def _progress_tqdm_class(
    progress_callback: ProgressCallback | None,
    should_cancel: CancelCallback | None,
) -> type[hf_tqdm]:
    class CallbackTqdm(hf_tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)
            self._report_progress()

        def display(self, msg=None, pos=None) -> None:
            # Keep server logs clean while retaining tqdm's byte counters.
            return None

        def _report_progress(self) -> None:
            if should_cancel and should_cancel():
                raise HuggingFaceDownloadCancelled("cancelled")
            if progress_callback:
                total = int(self.total) if self.total is not None else None
                progress_callback(int(self.n), total)

        def update(self, n=1):
            result = super().update(n)
            self._report_progress()
            return result

    return CallbackTqdm


def download_huggingface_file(
    url: str,
    destination: Path,
    *,
    token: str | None = None,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> Path:
    """Download one Hub file with hf-xet and atomically place it at destination."""
    ref = parse_huggingface_file_url(url)
    if ref is None:
        raise ValueError("URL is not a recognizable Hugging Face repository file")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    stage_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    stage_dir = destination.parent / f".{destination.name}.hf-stage-{stage_key}"
    marker = stage_dir / ".comfy-model-manager-stage"
    stage_dir.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        if any(stage_dir.iterdir()):
            raise RuntimeError(f"Refusing to reuse unrecognized staging directory: {stage_dir}")
        marker.write_text("native Hugging Face download staging\n", encoding="utf-8")

    tqdm_class = _progress_tqdm_class(progress_callback, should_cancel)
    downloaded = Path(
        hf_hub_download(
            repo_id=ref.repo_id,
            filename=ref.filename,
            revision=ref.revision,
            repo_type=ref.repo_type,
            local_dir=stage_dir,
            token=token or None,
            library_name="comfy-model-manager",
            library_version="0.1.0",
            tqdm_class=tqdm_class,
        )
    )

    if should_cancel and should_cancel():
        raise HuggingFaceDownloadCancelled("cancelled")
    if not downloaded.is_file():
        raise RuntimeError(f"Hugging Face download did not produce a file: {downloaded}")

    os.replace(downloaded, destination)
    try:
        if marker.is_file():
            shutil.rmtree(stage_dir)
    except OSError:
        # The completed model is already in place; stale staging metadata is
        # harmless and can be reused or removed during a later download.
        pass
    return destination
