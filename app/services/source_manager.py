"""
Source Manager for Hash -> URL metadata.
Stored in LAKE_MODELS_ROOT/.model_sources.json
"""

import json
from pathlib import Path
from typing import Dict, Optional
import aiofiles
from pydantic import BaseModel

from app.config import get_settings

class ModelSource(BaseModel):
    url: str
    added_at: str
    notes: Optional[str] = None
    filename_hint: Optional[str] = None

class SourceManager:
    def __init__(self):
        self._cache: Dict[str, ModelSource] = {}
        self._loaded = False
        self._file_path: Optional[Path] = None

    def _get_path(self) -> Path:
        settings = get_settings()
        return settings.lake_models_root / ".model_sources.json"

    async def load(self):
        """Load sources from disk."""
        path = self._get_path()
        if not path.exists():
            self._cache = {}
            self._loaded = True
            return

        try:
            async with aiofiles.open(path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
                self._cache = {
                    k: ModelSource(**v) for k, v in data.items()
                }
        except Exception as e:
            print(f"Error loading model sources: {e}")
            self._cache = {}
        
        self._loaded = True

    async def get_source(self, file_hash: str) -> Optional[ModelSource]:
        if not self._loaded:
            await self.load()
        return self._cache.get(file_hash)

    async def set_source(self, file_hash: str, source: ModelSource):
        if not self._loaded:
            await self.load()
        
        self._cache[file_hash] = source
        await self._save()

    async def _save(self):
        path = self._get_path()
        data = {k: v.model_dump() for k, v in self._cache.items()}
        try:
            async with aiofiles.open(path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Error saving model sources: {e}")

_source_manager = SourceManager()

def get_source_manager() -> SourceManager:
    return _source_manager
