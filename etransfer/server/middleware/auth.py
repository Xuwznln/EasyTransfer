"""Authentication middleware for FastAPI."""

from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from etransfer.common.constants import AUTH_HEADER


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Token-based authentication middleware.

    Validates API tokens in the X-API-Token header.
    Also accepts OAuth session tokens via Authorization: Bearer <token>
    or X-Session-Token header when user system is enabled.
    """

    def __init__(
        self,
        app,
        valid_tokens: list[str],
        exclude_paths: Optional[list[str]] = None,
        user_db=None,
    ):
        """Initialize auth middleware.

        Args:
            app: FastAPI application
            valid_tokens: List of valid API tokens
            exclude_paths: Paths to exclude from authentication
            user_db: Optional UserDB for session token validation
        """
        super().__init__(app)
        self.valid_tokens = set(valid_tokens)
        self.user_db = user_db
        self.exclude_paths = exclude_paths or [
            "/api/health",
            "/api/info",
            "/api/users/login",
            "/api/users/callback",
            "/docs",
            "/openapi.json",
            "/redoc",
        ]
        # Always exclude login-info (clients need this unauthenticated)
        if "/api/users/login-info" not in self.exclude_paths:
            self.exclude_paths.append("/api/users/login-info")

    @property
    def _active_tokens(self) -> set[str]:
        """Return the current set of valid tokens.

        Reads from ``app.state.settings`` when available so that
        hot-reloaded tokens take effect immediately.
        """
        return self.valid_tokens

    async def dispatch(self, request: Request, call_next: Callable):
        """Process request and validate token."""
        path = request.url.path

        # Skip authentication for excluded paths
        for exclude in self.exclude_paths:
            if path.startswith(exclude):
                return await call_next(request)

        # Skip authentication for OPTIONS requests (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Resolve current tokens (may have been hot-reloaded)
        _settings = getattr(request.app.state, "settings", None)
        active_tokens = set(_settings.auth_tokens) if _settings else self.valid_tokens

        # Skip if no tokens configured and no user_db (auth disabled)
        if not active_tokens and not self.user_db:
            return await call_next(request)

        # Try X-API-Token header first (static token auth)
        api_token = request.headers.get(AUTH_HEADER)
        if api_token and api_token in active_tokens:
            return await call_next(request)

        # Try session token (OAuth user system)
        session_token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            session_token = auth_header[7:]
        if not session_token:
            session_token = request.headers.get("X-Session-Token")

        if session_token and self.user_db:
            try:
                session = await self.user_db.get_session(session_token)
                if session:
                    user = await self.user_db.get_user(session.user_id)
                    if user and user.is_active:
                        # Attach user info to request state
                        request.state.user = user
                        request.state.session = session
                        return await call_next(request)
            except Exception:
                pass

        # If we have valid_tokens configured, require one
        if active_tokens or self.user_db:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "message": (f"Provide {AUTH_HEADER} header or " "Authorization: Bearer <session_token>"),
                },
            )

        return await call_next(request)
