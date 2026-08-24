from types import SimpleNamespace

import pytest

from app.database import init_db
from app.services.indexer import IndexerService


@pytest.mark.asyncio
async def test_scan_indexes_empty_folders_for_both_storage_sides(monkeypatch, tmp_path):
    local_root = tmp_path / "local"
    lake_root = tmp_path / "lake"
    local_root.mkdir()
    lake_root.mkdir()

    (local_root / "empty-local").mkdir()
    (local_root / "nested" / "empty-child").mkdir(parents=True)
    (local_root / "models").mkdir()
    (local_root / "models" / "model.safetensors").write_bytes(b"model")
    (lake_root / "empty-lake").mkdir()
    (lake_root / "nested" / "empty-child").mkdir(parents=True)

    db_path = tmp_path / "index.db"
    settings = SimpleNamespace(
        local_models_root=local_root,
        lake_models_root=lake_root,
        get_db_path=lambda: db_path,
    )
    monkeypatch.setattr("app.services.indexer.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.get_settings", lambda: settings)
    await init_db(db_path)

    indexer = IndexerService()
    await indexer.scan_side("local")
    await indexer.scan_side("lake")

    assert await indexer.get_folder_paths("local") == [
        "empty-local",
        "models",
        "nested",
        "nested/empty-child",
    ]
    assert await indexer.get_folder_paths("lake") == [
        "empty-lake",
        "nested",
        "nested/empty-child",
    ]
    assert await indexer.get_folders("local") == ["empty-local", "models", "nested"]
    assert await indexer.get_folders("local", parent="nested") == ["empty-child"]


@pytest.mark.asyncio
async def test_create_folder_mirrors_to_local_and_lake_and_updates_index(monkeypatch, tmp_path):
    local_root = tmp_path / "local"
    lake_root = tmp_path / "lake"
    local_root.mkdir()
    lake_root.mkdir()

    db_path = tmp_path / "index.db"
    settings = SimpleNamespace(
        local_models_root=local_root,
        lake_models_root=lake_root,
        get_db_path=lambda: db_path,
    )
    monkeypatch.setattr("app.services.indexer.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.get_settings", lambda: settings)
    await init_db(db_path)

    indexer = IndexerService()
    created = await indexer.create_folder("checkpoints", "wan_vace")

    assert created == {
        "relpath": "checkpoints/wan_vace",
        "local_created": True,
        "lake_created": True,
    }
    assert (local_root / "checkpoints" / "wan_vace").is_dir()
    assert (lake_root / "checkpoints" / "wan_vace").is_dir()
    assert await indexer.get_folder_paths("local") == ["checkpoints", "checkpoints/wan_vace"]
    assert await indexer.get_folder_paths("lake") == ["checkpoints", "checkpoints/wan_vace"]

    existing = await indexer.create_folder("checkpoints", "wan_vace")
    assert existing["local_created"] is False
    assert existing["lake_created"] is False


@pytest.mark.asyncio
async def test_create_folder_rejects_unsafe_child_name_before_touching_storage(monkeypatch, tmp_path):
    local_root = tmp_path / "local"
    lake_root = tmp_path / "lake"
    local_root.mkdir()
    lake_root.mkdir()

    db_path = tmp_path / "index.db"
    settings = SimpleNamespace(
        local_models_root=local_root,
        lake_models_root=lake_root,
        get_db_path=lambda: db_path,
    )
    monkeypatch.setattr("app.services.indexer.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.get_settings", lambda: settings)
    await init_db(db_path)

    indexer = IndexerService()
    with pytest.raises(ValueError, match="single, non-empty"):
        await indexer.create_folder("checkpoints", "nested/child")

    assert list(local_root.iterdir()) == []
    assert list(lake_root.iterdir()) == []
