from pathlib import Path

import bootstrapper


def test_bootstrapper_never_sends_hf_token_to_lookalike_domain(monkeypatch):
    monkeypatch.setattr(bootstrapper, "HF_API_KEY", "hf_secret")

    assert bootstrapper.auth_headers_for_source(
        "huggingface",
        "https://evil-huggingface.co/org/repo/resolve/main/model.bin",
    ) == {}
    assert bootstrapper.auth_headers_for_source(
        "huggingface",
        "https://huggingface.co/org/repo/resolve/main/model.bin",
    ) == {"Authorization": "Bearer hf_secret"}


def test_bootstrapper_cancellation_before_commit_keeps_destination_uncommitted(
    tmp_path,
    monkeypatch,
):
    cancelled = [False]

    def fake_hf_hub_download(**kwargs):
        downloaded = Path(kwargs["local_dir"]) / kwargs["filename"]
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        downloaded.write_bytes(b"model")
        return str(downloaded)

    def progress_callback(downloaded, total, pct):
        if downloaded == total == 5:
            cancelled[0] = True

    monkeypatch.setattr(bootstrapper, "hf_hub_download", fake_hf_hub_download)
    destination = tmp_path / "model.bin.part"

    ok, error = bootstrapper.native_huggingface_download(
        "https://huggingface.co/org/repo/resolve/main/model.bin",
        destination,
        task_id=1,
        should_cancel=lambda: cancelled[0],
        progress_callback=progress_callback,
    )

    assert ok is False
    assert error == "cancelled"
    assert destination.exists() is False
