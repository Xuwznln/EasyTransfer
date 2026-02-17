"""State storage backends."""

from easytransfer.server.services.backends.interface import StateBackend
from easytransfer.server.services.backends.memory import MemoryStateBackend
from easytransfer.server.services.backends.file import FileStateBackend

__all__ = [
    "StateBackend",
    "MemoryStateBackend",
    "FileStateBackend",
]

# Optional Redis backend
try:
    from easytransfer.server.services.backends.redis import RedisStateBackend
    __all__.append("RedisStateBackend")
except ImportError:
    pass

