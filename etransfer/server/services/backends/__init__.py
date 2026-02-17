"""State storage backends."""

from etransfer.server.services.backends.file import FileStateBackend
from etransfer.server.services.backends.interface import StateBackend
from etransfer.server.services.backends.memory import MemoryStateBackend

__all__ = [
    "StateBackend",
    "MemoryStateBackend",
    "FileStateBackend",
]

# Optional Redis backend
try:
    from etransfer.server.services.backends.redis import RedisStateBackend

    __all__.append("RedisStateBackend")
except ImportError:
    pass
