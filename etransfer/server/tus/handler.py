"""TUS protocol handler for FastAPI."""

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from etransfer.common.constants import CONTENT_TYPE_OFFSET, TUS_VERSION, UPLOAD_EXPIRATION_SECONDS, TusHeaders
from etransfer.server.tus.models import TusCapabilities, TusErrors, TusMetadata, TusUpload
from etransfer.server.tus.storage import TusStorage


def create_tus_router(
    storage: TusStorage,
    max_size: Optional[int] = None,
) -> APIRouter:
    """Create a TUS protocol router.

    Retention config (default_retention, default_retention_ttl,
    token_retention_policies) is read from ``request.app.state.settings``
    at request time so that hot-reloaded config takes effect immediately.

    Args:
        storage: TUS storage backend
        max_size: Maximum upload size (None = unlimited)

    Returns:
        FastAPI router with TUS endpoints
    """
    router = APIRouter(tags=["TUS"])
    capabilities = TusCapabilities(max_size=max_size)  # type: ignore[call-arg]

    def get_tus_headers() -> dict:
        """Get common TUS response headers."""
        headers = {
            TusHeaders.TUS_RESUMABLE: TUS_VERSION,
            TusHeaders.TUS_VERSION: TUS_VERSION,
        }
        return headers

    def validate_tus_version(request: Request) -> None:
        """Validate Tus-Resumable header."""
        tus_version = request.headers.get(TusHeaders.TUS_RESUMABLE)
        if tus_version and tus_version != TUS_VERSION:
            raise TusErrors.invalid_version()

    @router.options("/tus")
    @router.options("/tus/{file_id}")
    async def tus_options(request: Request) -> Response:
        """Handle OPTIONS request - return server capabilities."""
        headers = get_tus_headers()
        headers[TusHeaders.TUS_EXTENSION] = ",".join(capabilities.extensions)
        if capabilities.max_size:
            headers[TusHeaders.TUS_MAX_SIZE] = str(capabilities.max_size)

        return Response(
            status_code=204,
            headers=headers,
        )

    @router.post("/tus")
    async def tus_create(request: Request) -> Response:
        """Handle POST request - create new upload."""
        validate_tus_version(request)

        # Parse Upload-Length header
        upload_length_str = request.headers.get(TusHeaders.UPLOAD_LENGTH)
        if not upload_length_str:
            raise HTTPException(400, "Missing Upload-Length header")

        try:
            upload_length = int(upload_length_str)
        except ValueError:
            raise HTTPException(400, "Invalid Upload-Length header")

        # Check max single file size
        if max_size and upload_length > max_size:
            raise HTTPException(413, "Upload exceeds maximum size")

        # Note: Storage quota is checked per-chunk in PATCH, not here.
        # This allows creating the upload record even when quota is tight;
        # the client will be throttled chunk-by-chunk as needed.

        # Parse metadata
        metadata_header = request.headers.get(TusHeaders.UPLOAD_METADATA, "")
        try:
            if metadata_header:
                tus_metadata = TusMetadata.from_header(metadata_header)
            else:
                tus_metadata = TusMetadata(filename=f"upload_{uuid.uuid4().hex[:8]}")  # type: ignore[call-arg]
        except ValueError as e:
            raise HTTPException(400, f"Invalid Upload-Metadata: {e}")

        # Generate file ID
        file_id = uuid.uuid4().hex

        # Calculate expiration
        expires_at = datetime.utcnow() + timedelta(seconds=UPLOAD_EXPIRATION_SECONDS)

        # Determine retention policy: client metadata > token policy > server default
        retention = tus_metadata.retention
        retention_ttl = tus_metadata.retention_ttl

        # If client didn't specify, check token-level policy.
        # Read retention config from app.state.settings (hot-reloadable).
        if not retention:
            from etransfer.common.constants import AUTH_HEADER

            _settings = getattr(request.app.state, "settings", None)
            _def_ret = getattr(_settings, "default_retention", "permanent")
            _def_ttl = getattr(_settings, "default_retention_ttl", None)
            _tok_pol = getattr(_settings, "token_retention_policies", None) or {}

            token = request.headers.get(AUTH_HEADER, "")
            if token and token in _tok_pol:
                tp = _tok_pol[token]
                retention = tp.get("default_retention", _def_ret)
                if retention_ttl is None:
                    retention_ttl = tp.get("default_ttl", _def_ttl)
            else:
                retention = _def_ret
                if retention_ttl is None:
                    retention_ttl = _def_ttl

        if retention not in ("permanent", "download_once", "ttl"):
            retention = "permanent"

        # Create upload record
        upload = TusUpload(  # type: ignore[call-arg]
            file_id=file_id,
            filename=tus_metadata.filename,
            size=upload_length,
            offset=0,
            metadata={
                "filetype": tus_metadata.filetype,
                "checksum": tus_metadata.checksum,
                "retention": retention,
                "retention_ttl": retention_ttl,
            },
            expires_at=expires_at,
            storage_path=str(storage.get_file_path(file_id)),
            mime_type=tus_metadata.filetype,
            checksum=tus_metadata.checksum,
            retention=retention,
            retention_ttl=retention_ttl,
        )

        # Save to storage
        await storage.create_upload(upload)

        # Build location URL
        location = str(request.url).rstrip("/") + f"/{file_id}"

        # Check for creation-with-upload extension
        body = await request.body()
        if body and "creation-with-upload" in capabilities.extensions:
            content_type = request.headers.get(TusHeaders.CONTENT_TYPE)
            if content_type == CONTENT_TYPE_OFFSET:
                # Write initial data
                await storage.write_chunk(file_id, body, 0)
                upload.offset = len(body)
                upload.updated_at = datetime.utcnow()
                await storage.update_upload(upload)

        headers = get_tus_headers()
        headers[TusHeaders.LOCATION] = location
        headers[TusHeaders.UPLOAD_OFFSET] = str(upload.offset)
        if upload.expires_at:
            headers[TusHeaders.UPLOAD_EXPIRES] = upload.expires_at.isoformat()

        return Response(
            status_code=201,
            headers=headers,
        )

    @router.head("/tus/{file_id}")
    async def tus_head(file_id: str, request: Request) -> Response:
        """Handle HEAD request - get upload offset."""
        validate_tus_version(request)

        upload = await storage.get_upload(file_id)
        if not upload:
            raise HTTPException(404, "Upload not found")

        # Check expiration
        if upload.expires_at and upload.expires_at < datetime.utcnow():
            await storage.delete_upload(file_id)
            raise HTTPException(410, "Upload has expired")

        headers = get_tus_headers()
        headers[TusHeaders.UPLOAD_OFFSET] = str(upload.offset)
        headers[TusHeaders.UPLOAD_LENGTH] = str(upload.size)
        if upload.expires_at:
            headers[TusHeaders.UPLOAD_EXPIRES] = upload.expires_at.isoformat()

        # Add cache control to prevent caching
        headers["Cache-Control"] = "no-store"

        return Response(
            status_code=200,
            headers=headers,
        )

    @router.patch("/tus/{file_id}")
    async def tus_patch(file_id: str, request: Request) -> Response:
        """Handle PATCH request - upload chunk."""
        validate_tus_version(request)

        # Validate content type
        content_type = request.headers.get(TusHeaders.CONTENT_TYPE)
        if content_type != CONTENT_TYPE_OFFSET:
            raise HTTPException(415, f"Unsupported Content-Type: {content_type}")

        # Get upload record
        upload = await storage.get_upload(file_id)
        if not upload:
            raise HTTPException(404, "Upload not found")

        # Check expiration
        if upload.expires_at and upload.expires_at < datetime.utcnow():
            await storage.delete_upload(file_id)
            raise HTTPException(410, "Upload has expired")

        # Validate offset
        offset_str = request.headers.get(TusHeaders.UPLOAD_OFFSET)
        if not offset_str:
            raise HTTPException(400, "Missing Upload-Offset header")

        try:
            offset = int(offset_str)
        except ValueError:
            raise HTTPException(400, "Invalid Upload-Offset header")

        if offset != upload.offset:
            raise HTTPException(409, f"Offset mismatch: expected {upload.offset}, got {offset}")

        # Read chunk data
        chunk_data = await request.body()
        if not chunk_data:
            raise HTTPException(400, "Empty request body")

        # Check storage quota before writing
        allowed, usage = await storage.check_quota(len(chunk_data))
        if not allowed:
            headers = get_tus_headers()
            headers[TusHeaders.UPLOAD_OFFSET] = str(upload.offset)
            headers["Retry-After"] = "10"
            headers["X-Storage-Used"] = str(usage["used"])
            headers["X-Storage-Max"] = str(usage["max"])
            headers["X-Storage-Available"] = str(usage.get("available", 0))
            return Response(
                content="Storage quota exceeded. Retry after space is freed.",
                status_code=507,
                headers=headers,
            )

        # Check if chunk exceeds remaining size
        if offset + len(chunk_data) > upload.size:
            raise HTTPException(400, "Chunk exceeds upload size")

        # Validate checksum if provided
        checksum_header = request.headers.get(TusHeaders.UPLOAD_CHECKSUM)
        if checksum_header:
            algo, expected = checksum_header.split(" ", 1)
            if algo.lower() == "sha256":
                actual = hashlib.sha256(chunk_data).hexdigest()
            elif algo.lower() == "sha1":
                actual = hashlib.sha1(chunk_data).hexdigest()  # nosec B324
            elif algo.lower() == "md5":
                actual = hashlib.md5(chunk_data).hexdigest()  # nosec B324
            else:
                raise HTTPException(400, f"Unsupported checksum algorithm: {algo}")

            if actual != expected:
                raise HTTPException(460, "Checksum mismatch")

        # Write chunk to storage
        await storage.write_chunk(file_id, chunk_data, offset)

        # Update offset
        new_offset = offset + len(chunk_data)
        upload.offset = new_offset
        upload.updated_at = datetime.utcnow()

        # Check if upload is complete
        if new_offset >= upload.size:
            upload.is_final = True

        # Update upload state in Redis first (before finalize)
        await storage.update_upload(upload)

        # Finalize if complete (after state is saved)
        if upload.is_final:
            await storage.finalize_upload(file_id)

        headers = get_tus_headers()
        headers[TusHeaders.UPLOAD_OFFSET] = str(new_offset)
        if upload.expires_at:
            headers[TusHeaders.UPLOAD_EXPIRES] = upload.expires_at.isoformat()

        return Response(
            status_code=204,
            headers=headers,
        )

    @router.delete("/tus/{file_id}")
    async def tus_delete(file_id: str, request: Request) -> Response:
        """Handle DELETE request - terminate upload."""
        validate_tus_version(request)

        upload = await storage.get_upload(file_id)
        if not upload:
            raise HTTPException(404, "Upload not found")

        await storage.delete_upload(file_id)

        return Response(
            status_code=204,
            headers=get_tus_headers(),
        )

    return router


class TusHandler:
    """TUS protocol handler wrapper for easier integration."""

    def __init__(
        self,
        storage: TusStorage,
        max_size: Optional[int] = None,
    ) -> None:
        self.storage = storage
        self.max_size = max_size
        self.router = create_tus_router(storage, max_size)

    def get_router(self) -> APIRouter:
        """Get the TUS router."""
        return self.router
