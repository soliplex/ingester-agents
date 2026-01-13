"""
Authentication utilities for Soliplex Agents.

Supports two authentication methods:
1. API Key - Static key passed via Authorization: Bearer header (for programmatic access)
2. OAuth2 Proxy - User identity passed via X-Auth-Request-* headers (for UI access)

When auth is disabled (default), all requests are allowed.
"""

import logging
import secrets
from dataclasses import dataclass

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security import HTTPBearer

from soliplex.agents.config import Settings
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

# Bearer token authentication (integrates with OpenAPI docs)
bearer_scheme = HTTPBearer(auto_error=False)


def get_settings() -> Settings:
    """Return the settings instance."""
    return settings


@dataclass
class AuthenticatedUser:
    """Represents an authenticated user or API client."""

    identity: str  # Username or "api-client"
    email: str | None = None
    groups: list[str] | None = None
    method: str = "none"  # "api-key", "proxy", or "none"


def get_user_from_proxy_headers(request: Request) -> AuthenticatedUser | None:
    """
    Extract user identity from OAuth2 Proxy headers.

    Headers checked (in order of preference):
    - X-Auth-Request-User / X-Forwarded-User
    - X-Auth-Request-Email / X-Forwarded-Email
    - X-Auth-Request-Groups / X-Forwarded-Groups
    """
    user = request.headers.get("X-Auth-Request-User") or request.headers.get("X-Forwarded-User")
    if not user:
        return None

    email = request.headers.get("X-Auth-Request-Email") or request.headers.get("X-Forwarded-Email")
    groups_str = request.headers.get("X-Auth-Request-Groups") or request.headers.get("X-Forwarded-Groups")
    groups = groups_str.split(",") if groups_str else None

    return AuthenticatedUser(
        identity=user,
        email=email,
        groups=groups,
        method="proxy",
    )


def validate_api_key(api_key: str, settings: Settings) -> bool:
    """
    Validate an API key against the configured key.

    Uses constant-time comparison to prevent timing attacks.
    """
    if not settings.api_key:
        return False
    return secrets.compare_digest(api_key, settings.api_key)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AuthenticatedUser:
    """
    Dependency to get the current authenticated user.

    Authentication flow:
    1. If auth is disabled (api_key_enabled=False and auth_trust_proxy_headers=False),
       allow all requests with a default user.
    2. If Bearer token is provided and valid, authenticate as API client.
    3. If proxy headers are present and trusted, use proxy-provided identity.
    4. Otherwise, reject the request.

    Usage:
        @router.get("/protected")
        async def protected_route(user: AuthenticatedUser = Depends(get_current_user)):
            return {"user": user.identity}
    """
    # Check if any auth is enabled
    auth_enabled = settings.api_key_enabled or settings.auth_trust_proxy_headers

    if not auth_enabled:
        # Auth disabled - allow all requests
        return AuthenticatedUser(identity="anonymous", method="none")

    # Try Bearer token authentication first
    if credentials and settings.api_key_enabled:
        if validate_api_key(credentials.credentials, settings):
            logger.debug("Authenticated via Bearer token")
            return AuthenticatedUser(identity="api-client", method="api-key")
        else:
            logger.warning("Invalid Bearer token provided")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Try OAuth2 Proxy headers
    if settings.auth_trust_proxy_headers:
        proxy_user = get_user_from_proxy_headers(request)
        if proxy_user:
            logger.debug(f"Authenticated via proxy headers: {proxy_user.identity}")
            return proxy_user

    # No valid authentication found
    if settings.api_key_enabled and not settings.auth_trust_proxy_headers:
        # Only API key auth is enabled
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    elif settings.auth_trust_proxy_headers and not settings.api_key_enabled:
        # Only proxy auth is enabled
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    else:
        # Both are enabled but neither provided valid credentials
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (Bearer token or OAuth2 login)",
            headers={"WWW-Authenticate": "Bearer"},
        )
