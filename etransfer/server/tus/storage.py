"""TUS storage backend with pluggable state management."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiofiles  # type: ignore[import-untyped]
import aiofiles.os  # type: ignore[import-untyped]

from etransfer.common.constants import RedisKeys
from etransfer.server.tus.models import TusUpload

if TYPE_CHECKING:
    from etransfer.server.services.state import StateManager


class TusStorage:
    """TUS file storage backend with pluggable state management.

    This storage backend handles:
    - File data storage on disk
    - Upload state management via StateManager interface
    - Distributed locking for multi-worker support

    The state backend can be:
    - Memory: For development and single-process deployments
    - File: For persistence without external dependencies
    - Redis: For production multi-worker deployments
    """

    def __init__(
        self,
        storage_path: Path,
        state_manager: "StateManager",
        chunk_size: int = 4 * 1024 * 1024,
        max_storage_size: Optional[int] = None,
    ) -> None:
        """Initialize TUS storage.

        Args:
            storage_path: Base path for file storage
            state_manager: StateManager instance for state storage
            chunk_size: Default chunk size for operations
            max_storage_size: Maximum total storage in bytes (None = unlimited)
        """
        self.storage_path = Path(storage_path)
        self.state = state_manager
        self.chunk_size = chunk_size
        self.max_storage_size = max_storage_size

        # Ensure directories exist
        self.uploads_path = self.storage_path / "uploads"
        self.files_path = self.storage_path / "files"
        self.temp_path = self.storage_path / "temp"

    async def initialize(self) -> None:
        """Initialize storage directories."""
        for path in [self.uploads_path, self.files_path, self.temp_path]:
            path.mkdir(parents=True, exist_ok=True)

    def get_file_path(self, file_id: str) -> Path:
        """Get the storage path for a file."""
        return self.uploads_path / file_id

    def get_final_path(self, file_id: str, filename: str) -> Path:
        """Get the final storage path for a completed file."""
        return self.files_path / f"{file_id}_{filename}"

    def _upload_key(self, file_id: str) -> str:
        """Get key for upload state."""
        return f"{RedisKeys.UPLOAD_PREFIX}{file_id}"

    def _lock_key(self, file_id: str) -> str:
        """Get key for upload lock."""
        return f"{RedisKeys.LOCK_PREFIX}{file_id}"

    def _file_key(self, file_id: str) -> str:
        """Get key for file info."""
        return f"{RedisKeys.FILE_PREFIX}{file_id}"

    async def acquire_lock(self, file_id: str, timeout: int = 30) -> bool:
        """Acquire distributed lock for an upload.

        Args:
            file_id: Upload identifier
            timeout: Lock timeout in seconds

        Returns:
            True if lock acquired, False otherwise
        """
        lock_key = self._lock_key(file_id)
        # Use SET NX EX for atomic lock acquisition
        return await self.state.set(lock_key, "1", nx=True, ex=timeout)

    async def release_lock(self, file_id: str) -> None:
        """Release distributed lock for an upload."""
        lock_key = self._lock_key(file_id)
        await self.state.delete(lock_key)

    async def create_upload(self, upload: TusUpload) -> None:
        """Create a new upload record.

        Args:
            upload: TUS upload object
        """
        # Save state
        key = self._upload_key(upload.file_id)
        await self.state.set(
            key,
            json.dumps(upload.to_redis_dict()),
            ex=86400 * 7,  # 7 days TTL
        )

        # Create empty file (no pre-allocation to respect storage quota)
        file_path = self.get_file_path(upload.file_id)
        async with aiofiles.open(file_path, "wb") as _:
            pass  # Empty file; grows as chunks arrive

    async def get_upload(self, file_id: str) -> Optional[TusUpload]:
        """Get upload record by ID.

        Args:
            file_id: Upload identifier

        Returns:
            TusUpload object or None if not found
        """
        key = self._upload_key(file_id)
        data = await self.state.get(key)
        if not data:
            return None

        try:
            upload_dict = json.loads(data)
            return TusUpload.from_redis_dict(upload_dict)
        except (json.JSONDecodeError, ValueError):
            return None

    async def update_upload(self, upload: TusUpload) -> None:
        """Update upload record.

        Args:
            upload: TUS upload object with updated state
        """
        key = self._upload_key(upload.file_id)
        upload.updated_at = datetime.utcnow()
        await self.state.set(
            key,
            json.dumps(upload.to_redis_dict()),
            ex=86400 * 7,  # 7 days TTL
        )

    async def delete_upload(self, file_id: str) -> None:
        """Delete upload record and associated file.

        Args:
            file_id: Upload identifier
        """
        # Get upload info to find final path
        upload = await self.get_upload(file_id)

        # Delete from state (both upload and file keys)
        upload_key = self._upload_key(file_id)
        file_key = self._file_key(file_id)
        await self.state.delete(upload_key)
        await self.state.delete(file_key)

        # Delete file from uploads directory
        file_path = self.get_file_path(file_id)
        try:
            await aiofiles.os.remove(file_path)
        except FileNotFoundError:
            pass

        # Delete file from files directory (completed files)
        if upload and upload.filename:
            final_path = self.get_final_path(file_id, upload.filename)
            try:
                await aiofiles.os.remove(final_path)
            except FileNotFoundError:
                pass

        # Also scan files directory for any file starting with this file_id
        if self.files_path.exists():
            for f in self.files_path.iterdir():
                if f.is_file() and f.name.startswith(file_id):
                    try:
                        await aiofiles.os.remove(f)
                    except FileNotFoundError:
                        pass

        # Release any locks
        await self.release_lock(file_id)

    async def write_chunk(
        self,
        file_id: str,
        data: bytes,
        offset: int,
    ) -> int:
        """Write chunk data to file.

        Args:
            file_id: Upload identifier
            data: Chunk data bytes
            offset: Byte offset to write at

        Returns:
            Number of bytes written
        """
        file_path = self.get_file_path(file_id)

        # Acquire lock for concurrent write safety
        if not await self.acquire_lock(file_id):
            # Wait and retry
            await asyncio.sleep(0.1)
            if not await self.acquire_lock(file_id):
                raise RuntimeError("Could not acquire lock for write")

        try:
            # Use r+b if file exists and has content, else ab for appending
            # TUS writes sequentially, so offset == current file size
            mode = "r+b" if file_path.stat().st_size > 0 else "ab"
            async with aiofiles.open(file_path, mode) as f:  # type: ignore[call-overload]
                if mode == "r+b":
                    await f.seek(offset)
                await f.write(data)
                await f.flush()
            return len(data)
        finally:
            await self.release_lock(file_id)

    async def read_chunk(
        self,
        file_id: str,
        offset: int,
        length: int,
    ) -> bytes:
        """Read chunk data from file.

        Args:
            file_id: Upload/file identifier
            offset: Byte offset to read from
            length: Number of bytes to read

        Returns:
            Chunk data bytes
        """
        # Try uploads path first, then files path
        file_path = self.get_file_path(file_id)
        if not file_path.exists():
            # Check if it's a finalized file
            upload = await self.get_upload(file_id)
            if upload and upload.is_final:
                file_path = self.get_final_path(file_id, upload.filename)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_id}")

        async with aiofiles.open(file_path, "rb") as f:
            await f.seek(offset)
            return await f.read(length)  # type: ignore[no-any-return]

    async def get_available_size(self, file_id: str) -> int:
        """Get the size of data available for download.

        This returns the current upload offset, allowing
        partial downloads of in-progress uploads.

        Args:
            file_id: Upload/file identifier

        Returns:
            Available bytes for download
        """
        upload = await self.get_upload(file_id)
        if not upload:
            # Check if it's a finalized file
            file_key = self._file_key(file_id)
            file_data = await self.state.get(file_key)
            if file_data:
                info = json.loads(file_data)
                return info.get("size", 0)  # type: ignore[no-any-return]
            return 0

        return upload.offset

    async def finalize_upload(self, file_id: str) -> None:
        """Finalize a completed upload.

        Moves file from uploads to files directory and
        updates metadata.

        Args:
            file_id: Upload identifier
        """
        upload = await self.get_upload(file_id)
        if not upload:
            raise ValueError(f"Upload not found: {file_id}")

        if not upload.is_complete:
            raise ValueError(f"Upload not complete: {file_id}")

        # Move file to final location
        src_path = self.get_file_path(file_id)
        dst_path = self.get_final_path(file_id, upload.filename)

        if src_path.exists():
            await aiofiles.os.rename(str(src_path), str(dst_path))

        # Calculate retention_expires_at for TTL-based retention
        retention_expires_at = None
        if upload.retention == "ttl" and upload.retention_ttl:
            from datetime import timedelta

            retention_expires_at = datetime.utcnow() + timedelta(seconds=upload.retention_ttl)
            upload.retention_expires_at = retention_expires_at

        # Store file info
        file_info = {
            "file_id": file_id,
            "filename": upload.filename,
            "size": upload.size,
            "available_size": upload.size,
            "mime_type": upload.mime_type,
            "checksum": upload.checksum,
            "is_complete": True,
            "created_at": upload.created_at.isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "storage_path": str(dst_path),
            # Retention policy
            "retention": upload.retention,
            "retention_ttl": upload.retention_ttl,
            "retention_expires_at": (retention_expires_at.isoformat() if retention_expires_at else None),
            "download_count": 0,
            "owner_id": upload.owner_id,
        }

        file_key = self._file_key(file_id)
        await self.state.set(file_key, json.dumps(file_info))

        # Update upload record
        upload.is_final = True
        upload.storage_path = str(dst_path)
        await self.update_upload(upload)

    async def list_uploads(
        self,
        include_completed: bool = True,
        include_partial: bool = True,
    ) -> list[TusUpload]:
        """List all uploads.

        Args:
            include_completed: Include completed uploads
            include_partial: Include partial uploads

        Returns:
            List of TusUpload objects
        """
        pattern = f"{RedisKeys.UPLOAD_PREFIX}*"
        uploads = []

        # Scan all upload keys
        keys = await self.state.scan_keys(pattern)
        for key in keys:
            data = await self.state.get(key)
            if data:
                try:
                    upload = TusUpload.from_redis_dict(json.loads(data))
                    if upload.is_final and not include_completed:
                        continue
                    if not upload.is_final and not include_partial:
                        continue
                    uploads.append(upload)
                except (json.JSONDecodeError, ValueError):
                    continue

        return uploads

    async def list_files(self) -> list[dict]:
        """List all completed files.

        Returns:
            List of file info dicts
        """
        pattern = f"{RedisKeys.FILE_PREFIX}*"
        files = []

        keys = await self.state.scan_keys(pattern)
        for key in keys:
            data = await self.state.get(key)
            if data:
                try:
                    files.append(json.loads(data))
                except json.JSONDecodeError:
                    continue

        return files

    async def get_file_info(self, file_id: str) -> Optional[dict]:
        """Get file info by ID.

        Args:
            file_id: File identifier

        Returns:
            File info dict or None
        """
        file_key = self._file_key(file_id)
        data = await self.state.get(file_key)
        if data:
            return json.loads(data)  # type: ignore[no-any-return]

        # Try upload record
        upload = await self.get_upload(file_id)
        if upload:
            return {
                "file_id": file_id,
                "filename": upload.filename,
                "size": upload.size,
                "available_size": upload.offset,
                "mime_type": upload.mime_type,
                "checksum": upload.checksum,
                "is_complete": upload.is_complete,
                "created_at": upload.created_at.isoformat(),
                "retention": upload.retention,
                "retention_ttl": upload.retention_ttl,
                "retention_expires_at": (
                    upload.retention_expires_at.isoformat() if upload.retention_expires_at else None
                ),
                "download_count": upload.download_count,
                "owner_id": upload.owner_id,
            }

        return None

    async def record_download(self, file_id: str) -> dict:
        """Record a download and return retention info.

        For download_once files, marks for deletion.
        Returns dict with retention status.

        Args:
            file_id: File identifier

        Returns:
            Dict with 'should_delete', 'retention', 'download_count'
        """
        # Check file_key first (completed files)
        file_key = self._file_key(file_id)
        data = await self.state.get(file_key)
        if data:
            info = json.loads(data)
            info["download_count"] = info.get("download_count", 0) + 1
            await self.state.set(file_key, json.dumps(info))

            retention = info.get("retention", "permanent")
            return {
                "should_delete": retention == "download_once",
                "retention": retention,
                "download_count": info["download_count"],
            }

        # Check upload record (partial files)
        upload = await self.get_upload(file_id)
        if upload:
            upload.download_count += 1
            await self.update_upload(upload)
            return {
                "should_delete": upload.retention == "download_once",
                "retention": upload.retention,
                "download_count": upload.download_count,
            }

        return {"should_delete": False, "retention": "permanent", "download_count": 0}

    async def cleanup_expired(self, user_db: Any = None) -> int:
        """Clean up expired uploads and retention-expired files.

        Handles:
        - Incomplete uploads past their upload expiration
        - Completed files past their TTL retention_expires_at

        Args:
            user_db: Optional UserDB instance for updating user storage_used

        Returns:
            Number of items cleaned up
        """
        now = datetime.utcnow()
        cleaned = 0

        # Clean expired incomplete uploads
        uploads = await self.list_uploads(include_completed=False)
        for upload in uploads:
            if upload.expires_at and upload.expires_at < now:
                if user_db and upload.owner_id:
                    await user_db.update_storage_used(upload.owner_id, -upload.offset)
                await self.delete_upload(upload.file_id)
                cleaned += 1

        # Clean TTL-expired completed files
        files = await self.list_files()
        for f in files:
            expires = f.get("retention_expires_at")
            if expires:
                if isinstance(expires, str):
                    expires = datetime.fromisoformat(expires)
                if expires < now:
                    file_id = f["file_id"]
                    owner_id = f.get("owner_id")
                    file_size = f.get("size", 0)
                    if user_db and owner_id:
                        await user_db.update_storage_used(owner_id, -file_size)
                    await self.delete_upload(file_id)
                    cleaned += 1

        return cleaned

    async def get_storage_usage(self) -> dict:
        """Get current storage usage statistics.

        Returns:
            Dict with:
            - used: total bytes used on disk
            - max: max storage size (None if unlimited)
            - available: bytes available (None if unlimited)
            - usage_percent: percentage used (0 if unlimited)
            - files_count: number of completed files
            - uploads_count: number of in-progress uploads
            - is_full: True if storage is at capacity
        """
        # Calculate actual disk usage
        used = 0
        for path_dir in [self.uploads_path, self.files_path]:
            if path_dir.exists():
                for f in path_dir.iterdir():
                    if f.is_file():
                        used += f.stat().st_size

        files = await self.list_files()
        uploads = await self.list_uploads(include_completed=False)

        result = {
            "used": used,
            "max": self.max_storage_size,
            "files_count": len(files),
            "uploads_count": len(uploads),
        }

        if self.max_storage_size:
            available = max(0, self.max_storage_size - used)  # type: ignore[assignment]
            result["available"] = available
            result["usage_percent"] = (
                round((used / self.max_storage_size) * 100, 2)  # type: ignore[assignment]
                if self.max_storage_size > 0
                else 0
            )
            result["is_full"] = used >= self.max_storage_size
        else:
            result["available"] = None
            result["usage_percent"] = 0
            result["is_full"] = False

        return result

    async def check_quota(self, additional_bytes: int = 0) -> tuple[bool, dict]:
        """Check if storage quota allows the operation.

        Args:
            additional_bytes: Bytes about to be written

        Returns:
            (allowed, storage_info) tuple
        """
        usage = await self.get_storage_usage()
        if not self.max_storage_size:
            return True, usage

        would_use = usage["used"] + additional_bytes
        allowed = would_use <= self.max_storage_size
        return allowed, usage  # type: ignore[no-any-return]
