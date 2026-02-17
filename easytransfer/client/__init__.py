"""EasyTransfer client module - CLI and GUI tools."""

__all__ = [
    "EasyTransferClient",
    "EasyTransferUploader",
    "ChunkDownloader",
    "LocalCache",
]

# Lazy imports
def __getattr__(name):
    if name in ("EasyTransferClient", "EasyTransferUploader"):
        from easytransfer.client.tus_client import (
            EasyTransferClient,
            EasyTransferUploader,
        )
        return locals()[name]
    if name == "ChunkDownloader":
        from easytransfer.client.downloader import ChunkDownloader
        return ChunkDownloader
    if name == "LocalCache":
        from easytransfer.client.cache import LocalCache
        return LocalCache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

