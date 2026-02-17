"""Server services - traffic monitoring, IP management, state management."""

from etransfer.server.services.backends import FileStateBackend, MemoryStateBackend, StateBackend
from etransfer.server.services.ip_mgr import IPManager
from etransfer.server.services.state import (
    BackendType,
    StateManager,
    close_state_manager,
    create_state_manager,
    get_state_manager,
)
from etransfer.server.services.traffic import TrafficMonitor

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
    from etransfer.server.services.backends import RedisStateBackend

    __all__.append("RedisStateBackend")
except ImportError:
    pass
