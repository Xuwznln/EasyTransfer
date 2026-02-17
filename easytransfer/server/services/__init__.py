"""Server services - traffic monitoring, IP management, state management."""

from easytransfer.server.services.state import (
    StateManager,
    BackendType,
    get_state_manager,
    close_state_manager,
    create_state_manager,
)
from easytransfer.server.services.traffic import TrafficMonitor
from easytransfer.server.services.ip_mgr import IPManager
from easytransfer.server.services.backends import (
    StateBackend,
    MemoryStateBackend,
    FileStateBackend,
)

__all__ = [
    # State management
    "StateManager",
    "BackendType",
    "get_state_manager",
    "close_state_manager",
    "create_state_manager",
    # Backends
    "StateBackend",
    "MemoryStateBackend",
    "FileStateBackend",
    # Services
    "TrafficMonitor",
    "IPManager",
]

# Optional Redis backend
try:
    from easytransfer.server.services.backends import RedisStateBackend
    __all__.append("RedisStateBackend")
except ImportError:
    pass
