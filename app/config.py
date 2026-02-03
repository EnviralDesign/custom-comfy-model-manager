"""Application configuration using pydantic-settings."""

import os
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # Storage roots (required)
    local_models_root: Path
    lake_models_root: Path
    
    # Deletion policy (sync only - dedupe ignores these)
    local_allow_delete: bool = False
    lake_allow_delete: bool = False
    
    # Queue settings
    queue_concurrency: int = 1
    queue_retry_count: int = 3
    
    # Hashing
    hash_workers: int = 2
    
    # App data directory
    app_data_dir: Path | None = None
    
    # Server
    host: str = "127.0.0.1"
    port: int = 8420
    
    def get_app_data_dir(self) -> Path:
        """Get the app data directory, creating it if needed."""
        if self.app_data_dir:
            path = self.app_data_dir
        else:
            # Default to %APPDATA%\ComfyModelManager on Windows
            appdata = os.environ.get("APPDATA")
            if appdata:
                path = Path(appdata) / "ComfyModelManager"
            else:
                # Fallback for non-Windows or missing APPDATA
                path = Path.home() / ".comfy-model-manager"
        
        path.mkdir(parents=True, exist_ok=True)
        return path
    
    def get_db_path(self) -> Path:
        """Get the SQLite database path."""
        return self.get_app_data_dir() / "app.db"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
