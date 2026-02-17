"""Common utilities shared between client and server."""

__all__ = [
    "TUS_VERSION",
    "DEFAULT_CHUNK_SIZE",
    "TUS_EXTENSIONS",
    "FileInfo",
    "ServerInfo",
    "NetworkInterface",
    "UploadStatus",
]

# Lazy imports to avoid circular dependencies
def __getattr__(name):
    if name in ("TUS_VERSION", "DEFAULT_CHUNK_SIZE", "TUS_EXTENSIONS"):
        from easytransfer.common.constants import (
            TUS_VERSION,
            DEFAULT_CHUNK_SIZE,
            TUS_EXTENSIONS,
        )
        return locals()[name]
    if name in ("FileInfo", "ServerInfo", "NetworkInterface", "UploadStatus"):
        from easytransfer.common.models import (
            FileInfo,
            ServerInfo,
            NetworkInterface,
            UploadStatus,
        )
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
