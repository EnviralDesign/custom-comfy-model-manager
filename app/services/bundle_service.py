"""Bundle management service."""

from typing import Optional, List
from datetime import datetime, timezone
from pydantic import BaseModel
from urllib.parse import quote

from app.database import get_db


class BundleAsset(BaseModel):
    relpath: str
    hash: Optional[str] = None
    source_url_override: Optional[str] = None
    source_url: Optional[str] = None
    size: Optional[int] = None


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
            
            # Get assets with potential global source URLs
            cursor = await db.execute("""
                SELECT ba.relpath, ba.hash, ba.source_url_override,
                       COALESCE(su_hash.url, su_path.url) as global_source_url,
                       fi.size as size
                FROM bundle_assets ba
                LEFT JOIN source_urls su_hash ON su_hash.key = ba.hash AND ba.hash IS NOT NULL
                LEFT JOIN source_urls su_path ON su_path.key = 'relpath:' || ba.relpath
                LEFT JOIN (
                    SELECT relpath, MAX(size) as size
                    FROM file_index
                    GROUP BY relpath
                ) fi ON fi.relpath = ba.relpath
                WHERE ba.bundle_id = ?
                ORDER BY ba.relpath
            """, (row["id"],))
            
            assets = await cursor.fetchall()
            for asset_row in assets:
                bundle.assets.append(BundleAsset(
                    relpath=asset_row["relpath"],
                    hash=asset_row["hash"],
                    source_url_override=asset_row["source_url_override"],
                    source_url=asset_row["global_source_url"],
                    size=asset_row["size"],
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
                       MAX(hash) as hash
                FROM file_index 
                WHERE relpath LIKE ?
                GROUP BY relpath
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
        seen_relpaths = set()
        candidates = []
        
        async with get_db() as db:
            for bundle_name in bundle_names:
                bundle = await self.get_bundle(bundle_name)
                if not bundle:
                    continue
                
                for asset in bundle.assets:
                    if asset.relpath in seen_relpaths:
                        continue
                    seen_relpaths.add(asset.relpath)
                    
                    public_url = await self._resolve_public_url(db, asset)
                    local_url = await self._resolve_local_url(db, asset, server_base_url)
                    
                    if not public_url and not local_url:
                        continue
                    
                    candidates.append({
                        "relpath": asset.relpath,
                        "hash": asset.hash,
                        "size": asset.size,
                        "public_url": public_url,
                        "local_url": local_url,
                    })

        # Split items that have both public + local sources
        both = [c for c in candidates if c["public_url"] and c["local_url"]]
        local_selected = set()
        if both:
            def size_key(item):
                size = item.get("size")
                return (size is None, size or 0)

            both_sorted = sorted(both, key=size_key)
            # Smallest half go to local, largest half go to public
            local_count = max(1, len(both_sorted) // 2) if len(both_sorted) > 1 else 1
            local_selected = {c["relpath"] for c in both_sorted[:local_count]}

        resolved = []
        for c in candidates:
            url = None
            if c["public_url"] and c["local_url"]:
                url = c["local_url"] if c["relpath"] in local_selected else c["public_url"]
            elif c["public_url"]:
                url = c["public_url"]
            elif c["local_url"]:
                url = c["local_url"]
            
            if not url:
                continue
            
            resolved.append(ResolvedAsset(
                relpath=c["relpath"],
                url=url,
                hash=c["hash"],
                size=c["size"],
            ))

        return resolved
    
    async def _resolve_public_url(self, db, asset: BundleAsset) -> Optional[str]:
        """Resolve a public URL for an asset (override or source_urls)."""
        if asset.source_url_override:
            return asset.source_url_override
        
        if asset.hash:
            cursor = await db.execute(
                "SELECT url FROM source_urls WHERE key = ?",
                (asset.hash,)
            )
            row = await cursor.fetchone()
            if row:
                return row["url"]
        
        cursor = await db.execute(
            "SELECT url FROM source_urls WHERE key = ?",
            (f"relpath:{asset.relpath}",)
        )
        row = await cursor.fetchone()
        if row:
            return row["url"]
        
        return None

    async def _resolve_local_url(self, db, asset: BundleAsset, server_base_url: str) -> Optional[str]:
        """Resolve a local or lake stream URL for an asset if indexed."""
        base_url = server_base_url.rstrip("/")

        # Prefer local if present, else lake
        cursor = await db.execute(
            "SELECT 1 FROM file_index WHERE relpath = ? AND side = 'local' LIMIT 1",
            (asset.relpath,)
        )
        row = await cursor.fetchone()
        if row:
            return f"{base_url}/api/remote/assets/file?side=local&relpath={quote(asset.relpath, safe='')}"

        cursor = await db.execute(
            "SELECT 1 FROM file_index WHERE relpath = ? AND side = 'lake' LIMIT 1",
            (asset.relpath,)
        )
        row = await cursor.fetchone()
        if row:
            return f"{base_url}/api/remote/assets/file?side=lake&relpath={quote(asset.relpath, safe='')}"

        return None


# Singleton
_bundle_service: Optional[BundleService] = None

def get_bundle_service() -> BundleService:
    global _bundle_service
    if _bundle_service is None:
        _bundle_service = BundleService()
    return _bundle_service
