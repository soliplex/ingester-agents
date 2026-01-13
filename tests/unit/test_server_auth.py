"""Tests for soliplex.agents.server.auth module."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from soliplex.agents.config import Settings
from soliplex.agents.server.auth import AuthenticatedUser
from soliplex.agents.server.auth import get_current_user
from soliplex.agents.server.auth import get_user_from_proxy_headers
from soliplex.agents.server.auth import validate_api_key


class TestAuthenticatedUser:
    """Tests for AuthenticatedUser dataclass."""

    def test_authenticated_user_defaults(self):
        """Test AuthenticatedUser with default values."""
        user = AuthenticatedUser(identity="testuser")
        assert user.identity == "testuser"
        assert user.email is None
        assert user.groups is None
        assert user.method == "none"

    def test_authenticated_user_full(self):
        """Test AuthenticatedUser with all values."""
        user = AuthenticatedUser(
            identity="testuser",
            email="test@example.com",
            groups=["admin", "users"],
            method="api-key",
        )
        assert user.identity == "testuser"
        assert user.email == "test@example.com"
        assert user.groups == ["admin", "users"]
        assert user.method == "api-key"


class TestValidateApiKey:
    """Tests for validate_api_key function."""

    def test_validate_api_key_valid(self):
        """Test validate_api_key with correct key."""
        settings = MagicMock(spec=Settings)
        settings.api_key = "secret-key-123"

        result = validate_api_key("secret-key-123", settings)
        assert result is True

    def test_validate_api_key_invalid(self):
        """Test validate_api_key with incorrect key."""
        settings = MagicMock(spec=Settings)
        settings.api_key = "secret-key-123"

        result = validate_api_key("wrong-key", settings)
        assert result is False

    def test_validate_api_key_no_key_configured(self):
        """Test validate_api_key when no key is configured."""
        settings = MagicMock(spec=Settings)
        settings.api_key = None

        result = validate_api_key("any-key", settings)
        assert result is False

    def test_validate_api_key_empty_key(self):
        """Test validate_api_key with empty configured key."""
        settings = MagicMock(spec=Settings)
        settings.api_key = ""

        # Empty string is falsy, so validate_api_key returns False
        result = validate_api_key("", settings)
        assert result is False


class TestGetUserFromProxyHeaders:
    """Tests for get_user_from_proxy_headers function."""

    def test_proxy_headers_x_auth_request_user(self):
        """Test extraction using X-Auth-Request-User header."""
        request = MagicMock()
        headers = {
            "X-Auth-Request-User": "proxyuser",
            "X-Auth-Request-Email": "proxy@example.com",
            "X-Auth-Request-Groups": "admin,users",
        }
        request.headers.get = lambda k: headers.get(k)

        result = get_user_from_proxy_headers(request)

        assert result is not None
        assert result.identity == "proxyuser"
        assert result.email == "proxy@example.com"
        assert result.groups == ["admin", "users"]
        assert result.method == "proxy"

    def test_proxy_headers_x_forwarded_user(self):
        """Test extraction using X-Forwarded-User header."""
        request = MagicMock()
        headers = {
            "X-Forwarded-User": "forwardeduser",
            "X-Forwarded-Email": "forwarded@example.com",
        }
        request.headers.get = lambda k: headers.get(k)

        result = get_user_from_proxy_headers(request)

        assert result is not None
        assert result.identity == "forwardeduser"
        assert result.email == "forwarded@example.com"
        assert result.groups is None
        assert result.method == "proxy"

    def test_proxy_headers_no_user(self):
        """Test returns None when no user header present."""
        request = MagicMock()
        request.headers.get = lambda k: None

        result = get_user_from_proxy_headers(request)

        assert result is None

    def test_proxy_headers_only_user(self):
        """Test extraction with only user header."""
        request = MagicMock()
        headers = {"X-Auth-Request-User": "simpleuser"}
        request.headers.get = lambda k: headers.get(k)

        result = get_user_from_proxy_headers(request)

        assert result is not None
        assert result.identity == "simpleuser"
        assert result.email is None
        assert result.groups is None


class TestGetCurrentUser:
    """Tests for get_current_user dependency."""

    @pytest.mark.asyncio
    async def test_auth_disabled_returns_anonymous(self):
        """Test returns anonymous user when auth is disabled."""
        request = MagicMock()
        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = False
        settings.auth_trust_proxy_headers = False

        result = await get_current_user(request, credentials=None, settings=settings)

        assert result.identity == "anonymous"
        assert result.method == "none"

    @pytest.mark.asyncio
    async def test_valid_api_key_returns_api_client(self):
        """Test returns api-client user with valid API key."""
        request = MagicMock()
        credentials = MagicMock()
        credentials.credentials = "valid-api-key"

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = True
        settings.auth_trust_proxy_headers = False
        settings.api_key = "valid-api-key"

        result = await get_current_user(request, credentials=credentials, settings=settings)

        assert result.identity == "api-client"
        assert result.method == "api-key"

    @pytest.mark.asyncio
    async def test_invalid_api_key_raises_401(self):
        """Test raises 401 with invalid API key."""
        request = MagicMock()
        credentials = MagicMock()
        credentials.credentials = "invalid-key"

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = True
        settings.auth_trust_proxy_headers = False
        settings.api_key = "valid-api-key"

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, credentials=credentials, settings=settings)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_proxy_headers_auth(self):
        """Test authentication via proxy headers."""
        request = MagicMock()
        headers = {
            "X-Auth-Request-User": "proxyuser",
            "X-Auth-Request-Email": "proxy@example.com",
        }
        request.headers.get = lambda k: headers.get(k)

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = False
        settings.auth_trust_proxy_headers = True

        result = await get_current_user(request, credentials=None, settings=settings)

        assert result.identity == "proxyuser"
        assert result.method == "proxy"

    @pytest.mark.asyncio
    async def test_api_key_only_no_credentials_raises_401(self):
        """Test raises 401 when API key required but not provided."""
        request = MagicMock()
        request.headers.get = lambda k: None

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = True
        settings.auth_trust_proxy_headers = False
        settings.api_key = "valid-api-key"

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, credentials=None, settings=settings)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Bearer token required"

    @pytest.mark.asyncio
    async def test_proxy_only_no_headers_raises_401(self):
        """Test raises 401 when proxy auth required but no headers."""
        request = MagicMock()
        request.headers.get = lambda k: None

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = False
        settings.auth_trust_proxy_headers = True

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, credentials=None, settings=settings)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Authentication required"

    @pytest.mark.asyncio
    async def test_both_auth_methods_no_valid_credentials(self):
        """Test raises 401 when both methods enabled but no valid credentials."""
        request = MagicMock()
        request.headers.get = lambda k: None

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = True
        settings.auth_trust_proxy_headers = True
        settings.api_key = "valid-api-key"

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, credentials=None, settings=settings)

        assert exc_info.value.status_code == 401
        assert "Bearer token or OAuth2 login" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_api_key_takes_precedence_over_proxy(self):
        """Test API key auth is checked before proxy headers."""
        request = MagicMock()
        headers = {"X-Auth-Request-User": "proxyuser"}
        request.headers.get = lambda k: headers.get(k)

        credentials = MagicMock()
        credentials.credentials = "valid-api-key"

        settings = MagicMock(spec=Settings)
        settings.api_key_enabled = True
        settings.auth_trust_proxy_headers = True
        settings.api_key = "valid-api-key"

        result = await get_current_user(request, credentials=credentials, settings=settings)

        # Should use API key, not proxy
        assert result.identity == "api-client"
        assert result.method == "api-key"
