"""TUS protocol specific models."""

import base64
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RetentionPolicy(str, Enum):
    """File retention policy after upload completes."""

    PERMANENT = "permanent"  # Keep forever (until manual delete)
    DOWNLOAD_ONCE = "download_once"  # Delete after first complete download (阅后即焚)
    TTL = "ttl"  # Delete after TTL expires (定时过期)


class TusMetadata(BaseModel):
    """TUS Upload Metadata parsed from Upload-Metadata header."""

    filename: str = Field(..., description="Original filename")
    filetype: Optional[str] = Field(None, description="MIME type")
    checksum: Optional[str] = Field(None, description="File checksum")
    retention: Optional[str] = Field(None, description="Retention policy: permanent/download_once/ttl")
    retention_ttl: Optional[int] = Field(None, description="TTL in seconds (for ttl policy)")

    @classmethod
    def from_header(cls, header_value: str) -> "TusMetadata":
        """Parse Upload-Metadata header value.

        Format: key1 base64value1,key2 base64value2,...
        """
        metadata = {}
        if not header_value:
            raise ValueError("Empty metadata header")

        for item in header_value.split(","):
            item = item.strip()
            if not item:
                continue

            parts = item.split(" ", 1)
            key = parts[0].strip()

            if len(parts) == 2:
                try:
                    value = base64.b64decode(parts[1].strip()).decode("utf-8")
                except Exception:
                    value = parts[1].strip()
            else:
                value = ""

            metadata[key] = value

        if "filename" not in metadata:
            raise ValueError("filename is required in metadata")

        return cls(
            filename=metadata.get("filename", ""),
            filetype=metadata.get("filetype"),
            checksum=metadata.get("checksum"),
            retention=metadata.get("retention"),
            retention_ttl=(int(metadata["retention_ttl"]) if metadata.get("retention_ttl") else None),
        )

    def to_header(self) -> str:
        """Convert to Upload-Metadata header value."""
        items = []
        items.append(f"filename {base64.b64encode(self.filename.encode()).decode()}")

        if self.filetype:
            items.append(f"filetype {base64.b64encode(self.filetype.encode()).decode()}")

        if self.checksum:
            items.append(f"checksum {base64.b64encode(self.checksum.encode()).decode()}")

        if self.retention:
            items.append(f"retention {base64.b64encode(self.retention.encode()).decode()}")

        if self.retention_ttl is not None:
            items.append(f"retention_ttl {base64.b64encode(str(self.retention_ttl).encode()).decode()}")

        return ",".join(items)


class TusUpload(BaseModel):
    """TUS Upload state stored in Redis."""

    file_id: str = Field(..., description="Unique upload identifier")
    filename: str = Field(..., description="Original filename")
    size: int = Field(..., description="Total file size")
    offset: int = Field(0, description="Current upload offset")
    metadata: dict = Field(default_factory=dict, description="Upload metadata")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = Field(None, description="Upload expiration time")
    is_final: bool = Field(False, description="Whether upload is complete")
    storage_path: str = Field(..., description="Path to stored file")
    checksum: Optional[str] = Field(None, description="File checksum")
    mime_type: Optional[str] = Field(None, description="MIME type")

    # Retention policy
    retention: str = Field("permanent", description="Retention: permanent/download_once/ttl")
    retention_ttl: Optional[int] = Field(None, description="TTL in seconds (for ttl policy)")
    retention_expires_at: Optional[datetime] = Field(
        None, description="When the file should be deleted (set after upload completes)"
    )
    download_count: int = Field(0, description="Number of completed downloads")

    @property
    def is_complete(self) -> bool:
        """Check if upload is complete."""
        return self.offset >= self.size

    @property
    def remaining(self) -> int:
        """Get remaining bytes to upload."""
        return max(0, self.size - self.offset)

    def to_redis_dict(self) -> dict:
        """Convert to dict for Redis storage."""
        data = self.model_dump()
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        if self.expires_at:
            data["expires_at"] = self.expires_at.isoformat()
        if self.retention_expires_at:
            data["retention_expires_at"] = self.retention_expires_at.isoformat()
        return data

    @classmethod
    def from_redis_dict(cls, data: dict) -> "TusUpload":
        """Create from Redis dict."""
        for dt_field in ("created_at", "updated_at", "expires_at", "retention_expires_at"):
            if data.get(dt_field) and isinstance(data[dt_field], str):
                data[dt_field] = datetime.fromisoformat(data[dt_field])
        return cls(**data)


class TusCapabilities(BaseModel):
    """TUS server capabilities."""

    version: str = Field("1.0.0", description="TUS protocol version")
    extensions: list[str] = Field(
        default_factory=lambda: [
            "creation",
            "creation-with-upload",
            "termination",
            "checksum",
            "expiration",
        ]
    )
    max_size: Optional[int] = Field(None, description="Maximum upload size")
    checksum_algorithms: list[str] = Field(default_factory=lambda: ["sha1", "sha256", "md5"])


class TusError(Exception):
    """TUS protocol error."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class TusErrors:
    """Common TUS errors."""

    @staticmethod
    def invalid_version() -> TusError:
        return TusError(412, "Precondition Failed: Invalid Tus-Resumable header")

    @staticmethod
    def upload_not_found() -> TusError:
        return TusError(404, "Upload not found")

    @staticmethod
    def invalid_offset() -> TusError:
        return TusError(409, "Conflict: Upload offset mismatch")

    @staticmethod
    def invalid_content_type() -> TusError:
        return TusError(415, "Unsupported Media Type")

    @staticmethod
    def upload_too_large() -> TusError:
        return TusError(413, "Request Entity Too Large")

    @staticmethod
    def upload_expired() -> TusError:
        return TusError(410, "Gone: Upload has expired")

    @staticmethod
    def checksum_mismatch() -> TusError:
        return TusError(460, "Checksum Mismatch")

    @staticmethod
    def missing_header(header: str) -> TusError:
        return TusError(400, f"Bad Request: Missing required header {header}")
