"""FastAPI server main entry point."""

import asyncio
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from etransfer.server.config import ServerSettings, load_server_settings, reload_hot_settings
from etransfer.server.middleware.auth import TokenAuthMiddleware
from etransfer.server.routes.auth import create_auth_router
from etransfer.server.routes.files import create_files_router
from etransfer.server.routes.info import create_info_router
from etransfer.server.services.ip_mgr import IPManager
from etransfer.server.services.state import BackendType, close_state_manager, get_state_manager
from etransfer.server.services.traffic import TrafficMonitor
from etransfer.server.tus.handler import TusHandler
from etransfer.server.tus.storage import TusStorage

# Global instances
_storage: Optional[TusStorage] = None
_traffic_monitor: Optional[TrafficMonitor] = None
_ip_manager: Optional[IPManager] = None
_user_db = None  # Optional[UserDB]
_oidc_provider = None  # Optional[OIDCProvider]


def _create_user_db(settings: ServerSettings) -> Any:
    """Create UserDB with the appropriate SQLAlchemy async URL."""
    from etransfer.server.auth.db import UserDB, build_database_url

    url = build_database_url(
        backend=settings.user_db_backend,
        sqlite_path=settings.user_db_path,
        storage_path=str(settings.storage_path),
        mysql_host=settings.mysql_host,
        mysql_port=settings.mysql_port,
        mysql_user=settings.mysql_user,
        mysql_password=settings.mysql_password,
        mysql_database=settings.mysql_database,
    )
    return UserDB(url)


def _propagate_hot_changes(
    app: FastAPI,
    settings: ServerSettings,
    changes: dict[str, tuple],
) -> None:
    """Push hot-reloaded settings into live runtime objects."""
    if "max_storage_size" in changes and _storage:
        _storage.max_storage_size = settings.max_storage_size

    # Role quotas (used by user system router via app.state.parsed_role_quotas)
    if "role_quotas" in changes:
        from etransfer.server.auth.models import RoleQuota

        parsed = {}
        for role_name, qdict in settings.role_quotas.items():
            if isinstance(qdict, dict):
                parsed[role_name] = RoleQuota(**qdict)
            else:
                parsed[role_name] = qdict
        app.state.parsed_role_quotas = parsed


def create_app(settings: Optional[ServerSettings] = None) -> FastAPI:
    """Create and configure FastAPI application.

    Args:
        settings: Server settings (None = load from env/config via auto-discovery)

    Returns:
        Configured FastAPI application
    """
    global _storage, _traffic_monitor, _ip_manager, _oidc_provider  # noqa: F824

    if settings is None:
        settings = load_server_settings()

    # Create app
    app = FastAPI(
        title="EasyTransfer Server",
        description="TUS-based file transfer server with chunked upload/download",
        version="0.1.0",
    )

    # Store settings in app.state so all components can read hot fields
    # at request time and the reload endpoint can update them in place.
    app.state.settings = settings
    app.state.config_mtime = _get_config_mtime(settings)

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "Tus-Resumable",
            "Tus-Version",
            "Tus-Extension",
            "Tus-Max-Size",
            "Upload-Offset",
            "Upload-Length",
            "Upload-Metadata",
            "Upload-Expires",
            "Location",
            "Content-Range",
            "X-Retention-Policy",
            "X-Retention-Expires",
            "X-Retention-Warning",
            "X-Download-Count",
        ],
    )

    # Add auth middleware if enabled
    if settings.auth_enabled and (settings.auth_tokens or settings.user_system_enabled):

        class UserDBLazy:
            """Lazy proxy that resolves to _user_db at call time."""

            async def get_session(self, token: str) -> Any:
                return await _user_db.get_session(token) if _user_db else None  # type: ignore[attr-defined]

            async def get_user(self, user_id: int) -> Any:
                return await _user_db.get_user(user_id) if _user_db else None  # type: ignore[attr-defined]

        app.add_middleware(
            TokenAuthMiddleware,
            valid_tokens=settings.auth_tokens,
            user_db=UserDBLazy() if settings.user_system_enabled else None,
        )

    @app.on_event("startup")
    async def startup_event() -> None:
        """Initialize services on startup."""
        global _storage, _traffic_monitor, _ip_manager, _user_db

        backend_type = BackendType(settings.state_backend)

        state_manager = await get_state_manager(
            backend_type=backend_type,
            storage_path=settings.storage_path,
            redis_url=settings.redis_url,
        )

        _storage = TusStorage(
            storage_path=settings.storage_path,
            state_manager=state_manager,
            chunk_size=settings.chunk_size,
            max_storage_size=settings.max_storage_size,
        )
        await _storage.initialize()

        _traffic_monitor = TrafficMonitor(
            interfaces=settings.interfaces or None,
        )
        _traffic_monitor.start()

        _ip_manager = IPManager(
            interfaces=settings.interfaces or None,
            prefer_ipv4=settings.prefer_ipv4,
        )

        # Initialize user system if enabled
        if settings.user_system_enabled:
            _user_db = _create_user_db(settings)
            await _user_db.connect()
            if _oidc_provider:
                try:
                    await _oidc_provider.discover()
                except Exception as e:
                    print(f"[Server] OIDC discovery failed (will use defaults): {e}")
            print(f"[Server] User system enabled (OIDC + {settings.user_db_backend})")

        # Start background tasks
        asyncio.create_task(cleanup_loop(settings.cleanup_interval))
        if settings.config_watch:
            asyncio.create_task(config_watch_loop(app))

        print(f"[Server] Started on port {settings.port}")
        print(f"[Server] State backend: {backend_type.value}")
        if settings.max_storage_size:
            max_mb = settings.max_storage_size / (1024 * 1024)
            print(f"[Server] Storage quota: {max_mb:.0f} MB")
        else:
            print("[Server] Storage quota: unlimited")
        if settings.config_watch:
            print(f"[Server] Config watch enabled " f"(interval: {settings.config_watch_interval}s)")

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        """Cleanup on shutdown."""
        global _traffic_monitor, _user_db  # noqa: F824
        if _traffic_monitor:
            _traffic_monitor.stop()
        if _user_db:
            await _user_db.disconnect()
        await close_state_manager()
        print("[Server] Shutdown complete")

    # ── Proxies (resolve globals lazily after startup) ─────────

    class StorageProxy:
        def __getattr__(self, name: str) -> Any:
            if _storage is None:
                raise RuntimeError("Storage not initialized")
            return getattr(_storage, name)

    class TrafficProxy:
        def __getattr__(self, name: str) -> Any:
            if _traffic_monitor is None:
                raise RuntimeError("Traffic monitor not initialized")
            return getattr(_traffic_monitor, name)

    class IPProxy:
        def __getattr__(self, name: str) -> Any:
            if _ip_manager is None:
                raise RuntimeError("IP manager not initialized")
            return getattr(_ip_manager, name)

    storage_proxy = StorageProxy()

    # ── Register routes ────────────────────────────────────────

    # TUS handler — reads retention config from app.state.settings
    tus_handler = TusHandler(storage_proxy, settings.max_upload_size)  # type: ignore[arg-type]
    app.include_router(tus_handler.get_router())

    # File management
    app.include_router(create_files_router(storage_proxy))  # type: ignore[arg-type]

    # Server info — reads advertised_endpoints from app.state.settings
    app.include_router(
        create_info_router(
            storage_proxy,  # type: ignore[arg-type]
            TrafficProxy(),  # type: ignore[arg-type]
            IPProxy(),  # type: ignore[arg-type]
            max_upload_size=settings.max_upload_size,
            server_port=settings.port,
        )
    )

    # Auth routes (static token verification endpoint)
    app.include_router(create_auth_router(settings.auth_tokens))

    # User system routes (OIDC, roles, groups, quotas)
    if settings.user_system_enabled:
        from etransfer.server.auth.models import RoleQuota
        from etransfer.server.auth.oauth import OIDCProvider
        from etransfer.server.auth.routes import create_user_router

        class UserDBProxy:
            def __getattr__(self, name: str) -> Any:
                if _user_db is None:
                    raise RuntimeError("UserDB not initialized")
                return getattr(_user_db, name)

        # Build role quotas from config (stored in app.state for hot-reload)
        parsed_role_quotas: dict[str, RoleQuota] = {}
        for role_name, qdict in settings.role_quotas.items():
            if isinstance(qdict, dict):
                parsed_role_quotas[role_name] = RoleQuota(**qdict)
            else:
                parsed_role_quotas[role_name] = qdict
        app.state.parsed_role_quotas = parsed_role_quotas

        if settings.oidc_client_id and settings.oidc_client_secret:
            callback_url = settings.oidc_callback_url or f"http://localhost:{settings.port}/api/users/callback"
            _oidc_provider = OIDCProvider(
                issuer_url=settings.oidc_issuer_url,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                callback_url=callback_url,
                scope=settings.oidc_scope,
            )

        app.include_router(
            create_user_router(UserDBProxy(), _oidc_provider, parsed_role_quotas)  # type: ignore[arg-type]
        )

    # ── Admin: config reload endpoint ──────────────────────────

    @app.post("/api/admin/reload-config")
    async def reload_config(request: Request) -> dict[str, Any]:
        """Reload hot-reloadable config fields from the config file.

        Requires admin authentication (API token or OIDC admin role).
        Returns a diff of changed fields and lists which fields are
        hot-reloadable vs require a restart.
        """
        from etransfer.server.config import HOT_RELOADABLE_FIELDS

        _require_admin_access(request)

        changes = reload_hot_settings(settings)
        if changes:
            _propagate_hot_changes(app, settings, changes)
            app.state.config_mtime = _get_config_mtime(settings)
            change_summary = {k: {"old": _safe_repr(old), "new": _safe_repr(new)} for k, (old, new) in changes.items()}
            print(f"[Config] Hot-reloaded {len(changes)} field(s): " f"{', '.join(changes.keys())}")
        else:
            change_summary = {}

        return {  # type: ignore[no-any-return]
            "reloaded": bool(changes),
            "changes": change_summary,
            "hot_reloadable": sorted(HOT_RELOADABLE_FIELDS),
            "requires_restart": [
                "host",
                "port",
                "workers",
                "storage_path",
                "state_backend",
                "redis_url",
                "oidc_issuer_url",
                "oidc_client_id",
                "oidc_client_secret",
                "user_db_backend",
                "mysql_*",
            ],
            "note": (
                "host/port/workers 以及 OIDC、数据库等配置变更需要重启服务。"
                "无法在线增加新的监听 IP/端口——uvicorn 绑定在启动时确定。"
                "群组配额 (group quota) 存储在数据库中，通过 "
                "PUT /api/groups/{id}/quota 管理，天然即时生效。"
            ),
        }

    @app.get("/api/admin/config-status")
    async def config_status(request: Request) -> dict[str, Any]:
        """Show which config file is loaded and watch status."""
        _require_admin_access(request)

        config_path = getattr(settings, "_config_path", None)
        return {
            "config_file": str(config_path.resolve()) if config_path else None,  # type: ignore[assignment]  # type: ignore[assignment]
            "config_watch": settings.config_watch,
            "config_watch_interval": settings.config_watch_interval,
        }

    return app


# ── Helper functions ──────────────────────────────────────────


def _get_config_mtime(settings: ServerSettings) -> Optional[float]:
    config_path = getattr(settings, "_config_path", None)
    if config_path and config_path.exists():
        return config_path.stat().st_mtime  # type: ignore[no-any-return]
    return None


def _safe_repr(value: Any) -> str:
    """Produce a safe string repr for change diffs (truncate long values)."""
    s = repr(value)
    return s[:200] + "..." if len(s) > 200 else s


def _require_admin_access(request: Request) -> None:
    """Check that the request has admin-level access."""
    from etransfer.common.constants import AUTH_HEADER

    settings: ServerSettings = request.app.state.settings

    # Accept any configured API token as admin
    api_token = request.headers.get(AUTH_HEADER, "")
    if api_token and api_token in settings.auth_tokens:
        return

    # Accept OIDC admin user
    user = getattr(request.state, "user", None)
    if user and (getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin"):
        return

    raise HTTPException(403, "Admin access required")


async def cleanup_loop(interval: int) -> None:
    """Background task to cleanup expired uploads."""
    global _storage  # noqa: F824

    while True:
        try:
            await asyncio.sleep(interval)
            if _storage:
                cleaned = await _storage.cleanup_expired()
                if cleaned > 0:
                    print(f"Cleaned up {cleaned} expired uploads")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Cleanup error: {e}")


async def config_watch_loop(app: FastAPI) -> None:
    """Background task that watches the config file for changes."""
    settings: ServerSettings = app.state.settings
    interval = settings.config_watch_interval

    while True:
        try:
            await asyncio.sleep(interval)
            new_mtime = _get_config_mtime(settings)
            old_mtime = app.state.config_mtime

            if new_mtime and old_mtime and new_mtime != old_mtime:
                print("[Config] File change detected, hot-reloading...")
                changes = reload_hot_settings(settings)
                if changes:
                    _propagate_hot_changes(app, settings, changes)
                    print(f"[Config] Auto-reloaded {len(changes)} field(s): " f"{', '.join(changes.keys())}")
                app.state.config_mtime = new_mtime

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Config] Watch error: {e}")


def run_server(
    host: str = "0.0.0.0",  # nosec B104
    port: int = 8765,
    workers: int = 1,
    config_path: Optional[Path] = None,
    storage_path: Optional[Path] = None,
    state_backend: Optional[str] = None,
    redis_url: Optional[str] = None,
) -> None:
    """Run the server.

    Args:
        host: Bind host
        port: Bind port
        workers: Number of workers
        config_path: Path to config file (None = auto-discover)
        storage_path: Path to storage directory
        state_backend: State backend type (memory, file, redis)
        redis_url: Redis connection URL (if using redis backend)
    """
    settings = load_server_settings(config_path)

    # Override with command line args
    if host:
        settings.host = host
    if port:
        settings.port = port
    if workers:
        settings.workers = workers
    if storage_path:
        settings.storage_path = storage_path
    if state_backend:
        settings.state_backend = state_backend  # type: ignore[assignment]
    if redis_url:
        settings.redis_url = redis_url

    # Ensure storage directory exists
    settings.storage_path.mkdir(parents=True, exist_ok=True)

    app = create_app(settings)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
    )


# For uvicorn command line: uvicorn etransfer.server.main:app
app = create_app()


if __name__ == "__main__":
    run_server()
