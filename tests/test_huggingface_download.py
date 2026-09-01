from pathlib import Path

import pytest

from app.routers.remote_assets import _classify_source
from app.services.agent_tools import hf_resolve
from app.services import huggingface_download as hf_download
from app.services.downloader import _detect_provider


@pytest.mark.parametrize(
    ("url", "repo_id", "filename", "revision", "repo_type"),
    [
        (
            "https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/flux1-dev.safetensors?download=true",
            "black-forest-labs/FLUX.1-dev",
            "flux1-dev.safetensors",
            "main",
            None,
        ),
        (
            "https://huggingface.co/org/repo/blob/v1/models/model.safetensors",
            "org/repo",
            "models/model.safetensors",
            "v1",
            None,
        ),
        (
            "https://huggingface.co/datasets/org/repo/resolve/refs%2Fpr%2F7/data/file.bin",
            "org/repo",
            "data/file.bin",
            "refs/pr/7",
            "dataset",
        ),
    ],
)
def test_parse_huggingface_file_url(url, repo_id, filename, revision, repo_type):
    ref = hf_download.parse_huggingface_file_url(url)

    assert ref is not None
    assert ref.repo_id == repo_id
    assert ref.filename == filename
    assert ref.revision == revision
    assert ref.repo_type == repo_type


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/org/repo/resolve/main/model.bin",
        "https://huggingface.co/org/repo",
        "https://huggingface.co/org/repo/tree/main",
        "https://huggingface.co/org/repo/resolve/main/../secret",
    ],
)
def test_parse_huggingface_file_url_rejects_non_file_urls(url):
    assert hf_download.parse_huggingface_file_url(url) is None


def test_public_huggingface_source_is_not_marked_auth_required():
    host, provider, requires_auth = _classify_source(
        "https://huggingface.co/org/repo/resolve/main/model.safetensors"
    )

    assert host == "huggingface.co"
    assert provider == "huggingface"
    assert requires_auth is False


def test_lookalike_domain_is_not_treated_as_huggingface():
    url = "https://evil-huggingface.co/org/repo/resolve/main/model.bin"
    assert _detect_provider(url) == "generic"
    _, provider, _ = _classify_source(url)
    assert provider == "unknown"


def test_hf_resolve_encodes_revision_slashes_for_native_parser():
    result = hf_resolve(
        repo_id="org/repo",
        file_name="models/model.bin",
        revision="refs/pr/7",
        validate=False,
        api_key=None,
    )

    assert "/resolve/refs%2Fpr%2F7/" in result["url"]
    ref = hf_download.parse_huggingface_file_url(result["url"])
    assert ref is not None
    assert ref.revision == "refs/pr/7"
    assert ref.filename == "models/model.bin"


def test_native_download_moves_staged_file_and_reports_progress(tmp_path, monkeypatch):
    calls = {}
    progress = []

    def fake_hf_hub_download(**kwargs):
        calls.update(kwargs)
        bar = kwargs["tqdm_class"](total=10, initial=0)
        bar.update(4)
        bar.update(6)
        bar.close()
        downloaded = Path(kwargs["local_dir"]) / kwargs["filename"]
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"0123456789")
        return str(downloaded)

    monkeypatch.setattr(hf_download, "hf_hub_download", fake_hf_hub_download)
    destination = tmp_path / "renamed.safetensors"

    result = hf_download.download_huggingface_file(
        "https://huggingface.co/org/repo/resolve/main/models/original.safetensors",
        destination,
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert result == destination
    assert destination.read_bytes() == b"0123456789"
    assert calls["repo_id"] == "org/repo"
    assert calls["filename"] == "models/original.safetensors"
    assert calls["token"] is None
    assert progress[-1] == (10, 10)
    assert not list(tmp_path.glob(".renamed.safetensors.hf-stage-*"))


def test_xet_transfer_progress_reports_before_file_reconstruction():
    progress = []
    progress_bar = hf_download._progress_tqdm_class(
        lambda downloaded, total: progress.append((downloaded, total)),
        should_cancel=None,
    )(total=10, initial=0)

    try:
        progress_bar.update_transfer(4)
        assert progress[-1] == (4, 10)

        progress_bar.update(2)
        assert progress[-1] == (4, 10)

        progress_bar.update_transfer(4)
        assert progress[-1] == (8, 10)
    finally:
        progress_bar.close()


def test_native_download_passes_optional_token(tmp_path, monkeypatch):
    seen_token = []

    def fake_hf_hub_download(**kwargs):
        seen_token.append(kwargs["token"])
        downloaded = Path(kwargs["local_dir"]) / kwargs["filename"]
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"model")
        return str(downloaded)

    monkeypatch.setattr(hf_download, "hf_hub_download", fake_hf_hub_download)

    hf_download.download_huggingface_file(
        "https://huggingface.co/org/private/resolve/main/model.bin",
        tmp_path / "model.bin",
        token="hf_test",
    )

    assert seen_token == ["hf_test"]
