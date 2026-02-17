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
        from easytransfer.server.tus.handler import TusHandler
        return TusHandler
    if name == "TusStorage":
        from easytransfer.server.tus.storage import TusStorage
        return TusStorage
    if name in ("TusUpload", "TusMetadata"):
        from easytransfer.server.tus.models import TusUpload, TusMetadata
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

