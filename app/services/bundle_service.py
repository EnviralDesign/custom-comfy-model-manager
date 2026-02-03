"""Bundle management service."""

from typing import Optional, List
from datetime import datetime, timezone
from pydantic import BaseModel

from app.database import get_db
from app.config import get_settings


class BundleAsset(BaseModel):
    relpath: str
    hash: Optional[str] = None
    source_url_override: Optional[str] = None


class Bundle(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: str
    updated_at: str
    assets: List[BundleAsset] = []
    asset_count: int = 0


class ResolvedAsset(BaseModel):
    relpath: str
    url: str
    hash: Optional[str] = None
    size: Optional[int] = None


class BundleService:
    """Manages bundles and their assets."""
    
    async def list_bundles(self) -> List[Bundle]:
        """List all bundles with their asset counts."""
        async with get_db() as db:
            cursor = await db.execute("""
                SELECT b.*, COUNT(ba.id) as asset_count 
                FROM bundles b 
                LEFT JOIN bundle_assets ba ON b.id = ba.bundle_id 
                GROUP BY b.id 
                ORDER BY b.name
            """)
            bundles = []
            for row in await cursor.fetchall():
                bundle = Bundle(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    asset_count=row["asset_count"]
                )
                bundles.append(bundle)
            return bundles
    
    async def get_bundle(self, name: str) -> Optional[Bundle]:
        """Get a bundle by name with all its assets."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM bundles WHERE name = ?", (name,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            
            bundle = Bundle(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            
            # Get assets
            cursor = await db.execute(
                "SELECT relpath, hash, source_url_override FROM bundle_assets WHERE bundle_id = ? ORDER BY relpath",
                (row["id"],)
            )
            assets = await cursor.fetchall()
            for asset_row in assets:
                bundle.assets.append(BundleAsset(
                    relpath=asset_row["relpath"],
                    hash=asset_row["hash"],
                    source_url_override=asset_row["source_url_override"],
                ))
            
            bundle.asset_count = len(assets)
            return bundle
    
    async def create_bundle(self, name: str, description: Optional[str] = None) -> Bundle:
        """Create a new bundle."""
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO bundles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, description, now, now)
            )
            await db.commit()
            bundle_id = cursor.lastrowid
            
            return Bundle(
                id=bundle_id,
                name=name,
                description=description,
                created_at=now,
                updated_at=now,
            )
    
    async def update_bundle(self, name: str, new_name: Optional[str] = None, description: Optional[str] = None) -> Optional[Bundle]:
        """Update bundle metadata."""
        bundle = await self.get_bundle(name)
        if not bundle:
            return None
        
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            await db.execute(
                "UPDATE bundles SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                (new_name or bundle.name, description if description is not None else bundle.description, now, bundle.id)
            )
            await db.commit()
        
        return await self.get_bundle(new_name or name)
    
    async def delete_bundle(self, name: str) -> bool:
        """Delete a bundle and all its assets."""
        async with get_db() as db:
            cursor = await db.execute(
                "DELETE FROM bundles WHERE name = ?", (name,)
            )
            await db.commit()
            return cursor.rowcount > 0
    
    async def add_asset(self, bundle_name: str, relpath: str, hash: Optional[str] = None, source_url_override: Optional[str] = None) -> bool:
        """Add an asset to a bundle."""
        bundle = await self.get_bundle(bundle_name)
        if not bundle:
            return False
        
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO bundle_assets (bundle_id, relpath, hash, source_url_override) VALUES (?, ?, ?, ?)",
                    (bundle.id, relpath, hash, source_url_override)
                )
                await db.execute(
                    "UPDATE bundles SET updated_at = ? WHERE id = ?",
                    (now, bundle.id)
                )
                await db.commit()
                return True
            except Exception as e:
                print(f"Failed to add asset: {e}")
                return False

    async def add_folder(self, bundle_name: str, folder_path: str) -> int:
        """Add all files in a folder (recursive) to a bundle."""
        bundle = await self.get_bundle(bundle_name)
        if not bundle:
            return 0
            
        async with get_db() as db:
            # Find all files in index starting with folder_path
            search_pattern = f"{folder_path}/%" if folder_path else "%"
            cursor = await db.execute("""
                SELECT relpath, 
                       COALESCE(lake_hash, local_hash) as hash
                FROM file_index 
                WHERE relpath LIKE ?
            """, (search_pattern,))
            
            assets_to_add = await cursor.fetchall()
            if not assets_to_add:
                return 0
                
            now = datetime.now(timezone.utc).isoformat()
            
            # Batch add (INSERT OR REPLACE prevents duplicates if some files already in bundle)
            await db.executemany("""
                INSERT OR REPLACE INTO bundle_assets (bundle_id, relpath, hash)
                VALUES (?, ?, ?)
            """, [(bundle.id, a["relpath"], a["hash"]) for a in assets_to_add])
            
            await db.execute(
                "UPDATE bundles SET updated_at = ? WHERE id = ?",
                (now, bundle.id)
            )
            await db.commit()
            return len(assets_to_add)
    
    async def remove_asset(self, bundle_name: str, relpath: str) -> bool:
        """Remove an asset from a bundle."""
        bundle = await self.get_bundle(bundle_name)
        if not bundle:
            return False
        
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            cursor = await db.execute(
                "DELETE FROM bundle_assets WHERE bundle_id = ? AND relpath = ?",
                (bundle.id, relpath)
            )
            if cursor.rowcount > 0:
                await db.execute(
                    "UPDATE bundles SET updated_at = ? WHERE id = ?",
                    (now, bundle.id)
                )
            await db.commit()
            return cursor.rowcount > 0
    
    async def resolve_bundles(self, bundle_names: List[str], server_base_url: str) -> List[ResolvedAsset]:
        """
        Resolve multiple bundles to a list of downloadable assets.
        Deduplicates by relpath (union of all bundles).
        Returns best URL for each asset.
        """
        settings = get_settings()
        seen_relpaths = set()
        resolved = []
        
        async with get_db() as db:
            for bundle_name in bundle_names:
                bundle = await self.get_bundle(bundle_name)
                if not bundle:
                    continue
                
                for asset in bundle.assets:
                    if asset.relpath in seen_relpaths:
                        continue
                    seen_relpaths.add(asset.relpath)
                    
                    # Find best URL for this asset
                    url = await self._resolve_asset_url(db, asset, server_base_url)
                    if url:
                        # Get file size if available
                        size = None
                        cursor = await db.execute(
                            "SELECT size FROM file_index WHERE relpath = ? LIMIT 1",
                            (asset.relpath,)
                        )
                        row = await cursor.fetchone()
                        if row:
                            size = row["size"]
                        
                        resolved.append(ResolvedAsset(
                            relpath=asset.relpath,
                            url=url,
                            hash=asset.hash,
                            size=size,
                        ))
        
        return resolved
    
    async def _resolve_asset_url(self, db, asset: BundleAsset, server_base_url: str) -> Optional[str]:
        """
        Resolve the best URL for an asset.
        Priority: source_url_override > source_urls table > local server stream
        """
        # 1. Check override on the asset itself
        if asset.source_url_override:
            return asset.source_url_override
        
        # 2. Check source_urls table (by hash if available, else by relpath)
        if asset.hash:
            cursor = await db.execute(
                "SELECT url FROM source_urls WHERE key = ?",
                (asset.hash,)
            )
            row = await cursor.fetchone()
            if row:
                return row["url"]
        
        # Try by relpath
        cursor = await db.execute(
            "SELECT url FROM source_urls WHERE key = ?",
            (f"relpath:{asset.relpath}",)
        )
        row = await cursor.fetchone()
        if row:
            return row["url"]
        
        # 3. Fall back to local server streaming the file
        # Check if file exists in index
        cursor = await db.execute(
            "SELECT side FROM file_index WHERE relpath = ? LIMIT 1",
            (asset.relpath,)
        )
        row = await cursor.fetchone()
        if row:
            # File exists, serve from local server
            from urllib.parse import quote
            return f"{server_base_url}/api/remote/serve/{quote(asset.relpath, safe='')}"
        
        # File doesn't exist anywhere
        return None


# Singleton
_bundle_service: Optional[BundleService] = None

def get_bundle_service() -> BundleService:
    global _bundle_service
    if _bundle_service is None:
        _bundle_service = BundleService()
    return _bundle_service
