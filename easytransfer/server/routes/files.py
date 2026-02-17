"""File management API routes."""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from easytransfer.common.models import (
    FileInfo,
    FileListResponse,
    FileStatus,
    DownloadInfo,
    ErrorResponse,
)
from easytransfer.server.tus.storage import TusStorage


def create_files_router(storage: TusStorage) -> APIRouter:
    """Create file management router.

    Args:
        storage: TUS storage backend

    Returns:
        FastAPI router
    """
    router = APIRouter(prefix="/api/files", tags=["Files"])

    @router.get(
        "",
        response_model=FileListResponse,
        responses={500: {"model": ErrorResponse}},
    )
    async def list_files(
        page: int = Query(1, ge=1, description="Page number"),
        page_size: int = Query(20, ge=1, le=100, description="Page size"),
        include_partial: bool = Query(True, description="Include partial uploads"),
    ):
        """List available files.

        Returns files that are either complete or in-progress (partial).
        Partial files can still be downloaded for their uploaded portion.
        """
        try:
            # Get completed files
            files_data = await storage.list_files()

            # Get partial uploads if requested
            uploads = []
            if include_partial:
                uploads = await storage.list_uploads(
                    include_completed=False, include_partial=True
                )

            # Convert to FileInfo models
            all_files = []

            for f in files_data:
                all_files.append(
                    FileInfo(
                        file_id=f["file_id"],
                        filename=f["filename"],
                        size=f["size"],
                        mime_type=f.get("mime_type"),
                        checksum=f.get("checksum"),
                        status=FileStatus.COMPLETE,
                        uploaded_size=f["size"],
                        chunk_size=storage.chunk_size,
                        total_chunks=(f["size"] + storage.chunk_size - 1)
                        // storage.chunk_size,
                        uploaded_chunks=(f["size"] + storage.chunk_size - 1)
                        // storage.chunk_size,
                    )
                )

            for u in uploads:
                all_files.append(
                    FileInfo(
                        file_id=u.file_id,
                        filename=u.filename,
                        size=u.size,
                        mime_type=u.mime_type,
                        checksum=u.checksum,
                        status=FileStatus.PARTIAL,
                        uploaded_size=u.offset,
                        chunk_size=storage.chunk_size,
                        total_chunks=(u.size + storage.chunk_size - 1)
                        // storage.chunk_size,
                        uploaded_chunks=(u.offset + storage.chunk_size - 1)
                        // storage.chunk_size,
                        created_at=u.created_at,
                        updated_at=u.updated_at,
                        expires_at=u.expires_at,
                    )
                )

            # Sort by updated_at descending
            all_files.sort(key=lambda x: x.updated_at, reverse=True)

            # Paginate
            total = len(all_files)
            start = (page - 1) * page_size
            end = start + page_size
            paginated = all_files[start:end]

            return FileListResponse(
                files=paginated,
                total=total,
                page=page,
                page_size=page_size,
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to list files: {str(e)}")

    @router.get(
        "/{file_id}",
        response_model=FileInfo,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_file_info(file_id: str):
        """Get information about a specific file."""
        info = await storage.get_file_info(file_id)
        if not info:
            raise HTTPException(404, f"File not found: {file_id}")

        # Determine status
        is_complete = info.get("is_complete", False)
        status = FileStatus.COMPLETE if is_complete else FileStatus.PARTIAL
        uploaded_size = info["available_size"]

        # Include retention info in metadata
        file_metadata = {
            "retention": info.get("retention", "permanent"),
            "retention_ttl": info.get("retention_ttl"),
            "retention_expires_at": info.get("retention_expires_at"),
            "download_count": info.get("download_count", 0),
        }

        return FileInfo(
            file_id=info["file_id"],
            filename=info["filename"],
            size=info["size"],
            mime_type=info.get("mime_type"),
            checksum=info.get("checksum"),
            status=status,
            uploaded_size=uploaded_size,
            chunk_size=storage.chunk_size,
            total_chunks=(info["size"] + storage.chunk_size - 1) // storage.chunk_size,
            uploaded_chunks=(uploaded_size + storage.chunk_size - 1) // storage.chunk_size,
            metadata=file_metadata,
        )

    @router.get(
        "/{file_id}/download",
        responses={
            200: {"description": "File data"},
            206: {"description": "Partial content"},
            404: {"model": ErrorResponse},
            416: {"model": ErrorResponse},
        },
    )
    async def download_file(
        file_id: str, request: Request, background_tasks: BackgroundTasks
    ):
        """Download a file with HTTP Range support.

        Supports partial content requests for resumable downloads.
        Can download partially uploaded files up to their current offset.

        Retention policies:
        - download_once: file is deleted after this download completes
        - ttl: file is kept until retention_expires_at, shown in X-Retention-Expires header
        - permanent: file is kept indefinitely
        """
        # Get file info
        info = await storage.get_file_info(file_id)
        if not info:
            raise HTTPException(404, f"File not found: {file_id}")

        available_size = info["available_size"]
        total_size = info["size"]
        filename = info["filename"]
        mime_type = info.get("mime_type", "application/octet-stream")
        retention = info.get("retention", "permanent")

        # Parse Range header
        range_header = request.headers.get("Range")
        start = 0
        end = available_size - 1

        if range_header:
            try:
                range_spec = range_header.replace("bytes=", "")
                if "-" in range_spec:
                    parts = range_spec.split("-")
                    if parts[0]:
                        start = int(parts[0])
                    if parts[1]:
                        end = min(int(parts[1]), available_size - 1)
            except ValueError:
                raise HTTPException(416, "Invalid Range header")

            # Validate range
            if start >= available_size or start > end:
                raise HTTPException(
                    416,
                    f"Range not satisfiable. Available: 0-{available_size - 1}",
                )

        content_length = end - start + 1

        # Check if this is a full download (determines download_once trigger)
        is_full_download = (start == 0 and end == available_size - 1)

        async def generate_chunks():
            """Generate file chunks for streaming."""
            offset = start
            remaining = content_length

            while remaining > 0:
                chunk_size = min(storage.chunk_size, remaining)
                try:
                    chunk = await storage.read_chunk(file_id, offset, chunk_size)
                    yield chunk
                    offset += len(chunk)
                    remaining -= len(chunk)
                except FileNotFoundError:
                    break

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "X-Retention-Policy": retention,
        }

        # Add TTL expiration info
        retention_expires = info.get("retention_expires_at")
        if retention_expires:
            headers["X-Retention-Expires"] = str(retention_expires)

        # Add download_once warning
        if retention == "download_once" and is_full_download:
            headers["X-Retention-Warning"] = "File will be deleted after this download"

        download_count = info.get("download_count", 0) + 1
        headers["X-Download-Count"] = str(download_count)

        # Record download and schedule deletion if download_once
        if is_full_download:
            async def _after_download():
                result = await storage.record_download(file_id)
                if result["should_delete"]:
                    await storage.delete_upload(file_id)

            background_tasks.add_task(_after_download)

        # Return 206 for partial content
        if range_header or available_size < total_size:
            headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
            return StreamingResponse(
                generate_chunks(),
                status_code=206,
                media_type=mime_type,
                headers=headers,
            )

        return StreamingResponse(
            generate_chunks(),
            status_code=200,
            media_type=mime_type,
            headers=headers,
        )

    @router.get(
        "/{file_id}/info/download",
        response_model=DownloadInfo,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_download_info(file_id: str):
        """Get download information for a file.

        Returns metadata needed to plan a chunked download.
        """
        info = await storage.get_file_info(file_id)
        if not info:
            raise HTTPException(404, f"File not found: {file_id}")

        available_size = info["available_size"]

        return DownloadInfo(
            file_id=info["file_id"],
            filename=info["filename"],
            size=info["size"],
            available_size=available_size,
            mime_type=info.get("mime_type"),
            checksum=info.get("checksum"),
            supports_range=True,
        )

    @router.delete(
        "/{file_id}",
        responses={404: {"model": ErrorResponse}},
    )
    async def delete_file(file_id: str):
        """Delete a file.

        Removes both the file data and metadata.
        """
        info = await storage.get_file_info(file_id)
        if not info:
            raise HTTPException(404, f"File not found: {file_id}")

        await storage.delete_upload(file_id)

        return {"status": "deleted", "file_id": file_id}

    @router.post(
        "/cleanup",
        responses={200: {"description": "Cleanup result"}},
    )
    async def trigger_cleanup():
        """Manually trigger cleanup of expired uploads and TTL-expired files.

        This is useful for testing - normally cleanup runs periodically.
        """
        cleaned = await storage.cleanup_expired()
        return {
            "status": "ok",
            "cleaned": cleaned,
        }

    return router

