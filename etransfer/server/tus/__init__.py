"""TUS protocol implementation for FastAPI."""

__all__ = [
    "TusHandler",
    "TusStorage",
    "TusUpload",
    "TusMetadata",
]

# Lazy imports


def __getattr__(name):
    if name == "TusHandler":
        from etransfer.server.tus.handler import TusHandler

        return TusHandler
    if name == "TusStorage":
        from etransfer.server.tus.storage import TusStorage

        return TusStorage
    if name in ("TusUpload", "TusMetadata"):
        from etransfer.server.tus.models import TusMetadata, TusUpload

        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
