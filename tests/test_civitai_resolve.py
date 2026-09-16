import pytest

from app.services import civitai_api as civ
from app.services import url_utils


class FakeCivitaiClient:
    def __init__(self, *, base_url, api_key=None, timeout=10):
        self.base_url = base_url
        self.api_key = api_key
        self.last_error = None

    def get_model_version(self, model_version_id):
        if model_version_id != 2718980:
            self.last_error = "not found"
            return None
        return {
            "model": {"id": 1272455, "name": "Hyperdetailed Illustration"},
            "modelVersion": {
                "id": 2718980,
                "name": "v1",
                "files": [
                    {
                        "name": "hyperdetailed-illustration-v1.safetensors",
                        "size": 1234,
                        "metadata": {"size": "1234", "format": "SafeTensor"},
                        "downloadUrl": "https://civitai.com/api/download/models/1272455?modelVersionId=2718980",
                    }
                ],
            },
        }

    def get_model(self, model_id):
        if model_id != 1272455:
            self.last_error = "not found"
            return None
        return {
            "id": 1272455,
            "name": "Hyperdetailed Illustration",
            "modelVersions": [
                {
                    "id": 2718980,
                    "name": "v1",
                    "files": [
                        {
                            "name": "hyperdetailed-illustration-v1.safetensors",
                            "size": 1234,
                            "metadata": {"size": "1234", "format": "SafeTensor"},
                            "downloadUrl": "https://civitai.com/api/download/models/1272455?modelVersionId=2718980",
                        }
                    ],
                }
            ],
        }


def test_resolve_civitai_page_url_with_version_id(monkeypatch):
    monkeypatch.setattr(civ, "CivitaiClient", FakeCivitaiClient)
    url = "https://civitai.com/models/1272455/hyperdetailed-illustration?modelVersionId=2718980"

    resolved = civ.resolve_civitai_page_url(url, base_url="https://civitai.com", api_key="k")

    assert resolved == "https://civitai.com/api/download/models/1272455?modelVersionId=2718980"


def test_resolve_civitai_page_url_model_only(monkeypatch):
    monkeypatch.setattr(civ, "CivitaiClient", FakeCivitaiClient)

    resolved = civ.resolve_civitai_page_url(
        "https://civitai.com/models/1272455", base_url="https://civitai.com", api_key="k"
    )

    assert resolved == "https://civitai.com/api/download/models/1272455?modelVersionId=2718980"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/models/1272455",
        "https://civitai.com/api/download/models/1272455",
        "https://civitai.com/models/not-a-number",
        "https://civitai.com/profiles/someuser",
    ],
)
def test_resolve_civitai_page_url_returns_none_for_non_pages(url):
    assert civ.resolve_civitai_page_url(url, base_url="https://civitai.com", api_key="k") is None


def test_resolve_civitai_page_url_none_when_version_has_no_files(monkeypatch):
    class EmptyCivitaiClient(FakeCivitaiClient):
        def get_model_version(self, model_version_id):
            return {"modelVersion": {"id": model_version_id, "name": "v1", "files": []}}

    monkeypatch.setattr(civ, "CivitaiClient", EmptyCivitaiClient)

    url = "https://civitai.com/models/1272455/hyperdetailed-illustration?modelVersionId=2718980"
    assert civ.resolve_civitai_page_url(url, base_url="https://civitai.com", api_key="k") is None


def test_pick_primary_file_prefers_largest_safetensors():
    candidates = [
        civ.CivitaiFileCandidate("https://x/emb.pt", "emb.pt", 1, "m", 1, "v", {"size": "99999"}),
        civ.CivitaiFileCandidate("https://x/big.safetensors", "big.safetensors", 1, "m", 1, "v", {"size": "1000"}),
        civ.CivitaiFileCandidate("https://x/small.safetensors", "small.safetensors", 1, "m", 1, "v", {"size": "5"}),
    ]

    assert civ.pick_primary_file(candidates).file_name == "big.safetensors"
    assert civ.pick_primary_file([]) is None


def test_check_url_sync_reports_resolved_url(monkeypatch):
    monkeypatch.setattr(civ, "CivitaiClient", FakeCivitaiClient)
    monkeypatch.setattr(
        url_utils,
        "_probe_url",
        lambda target: {
            "ok": True,
            "status": 200,
            "size": 1234,
            "type": "application/octet-stream",
            "url": target,
            "is_webpage": False,
            "filename": "hyperdetailed-illustration-v1.safetensors",
        },
    )

    result = url_utils.check_url_sync(
        "https://civitai.com/models/1272455/hyperdetailed-illustration?modelVersionId=2718980"
    )

    assert result["ok"] is True
    assert result["resolved_url"] == "https://civitai.com/api/download/models/1272455?modelVersionId=2718980"
