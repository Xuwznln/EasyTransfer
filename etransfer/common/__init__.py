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


def __getattr__(name: str) -> object:
    if name in ("TUS_VERSION", "DEFAULT_CHUNK_SIZE", "TUS_EXTENSIONS"):
        from etransfer.common.constants import DEFAULT_CHUNK_SIZE, TUS_EXTENSIONS, TUS_VERSION

        return locals()[name]
    if name in ("FileInfo", "ServerInfo", "NetworkInterface", "UploadStatus"):
        from etransfer.common.models import FileInfo, NetworkInterface, ServerInfo, UploadStatus

        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
