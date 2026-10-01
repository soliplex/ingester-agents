"""POST a JSON notification to a webhook -- shared by the pre-run and post-process steps.

Webhook URLs and auth headers are often secrets in their own right (a Slack or
Teams incoming-webhook URL is a bearer credential), so each has a ``*_secret``
form naming a docker secret or environment variable, resolved with
:func:`~soliplex.agents.config.resolve_credential` exactly as an SCM
``auth_token`` is. The manifest then never holds the value itself.
"""

import logging
from typing import Any

import aiohttp

from soliplex.agents.config import resolve_credential
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

# Characters of a failed response body quoted in the error.
_BODY_QUOTE = 200


def resolve_target(
    url: str | None,
    url_secret: str | None,
    headers: dict[str, str] | None,
    secret_headers: dict[str, str] | None,
) -> tuple[str, dict[str, str]]:
    """The URL and headers to send, with every ``*_secret`` resolved.

    Raises:
        ValueError: unless exactly one of *url* / *url_secret* is given, or
            when a secret cannot be resolved.
    """
    if (url is None) == (url_secret is None):
        raise ValueError("webhook needs exactly one of 'url' or 'url_secret'")
    resolved_url = url if url is not None else resolve_credential(url_secret)
    resolved_headers = dict(headers or {})
    for name, secret in (secret_headers or {}).items():
        resolved_headers[name] = resolve_credential(secret)
    return resolved_url, resolved_headers


async def post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 10) -> int:
    """POST *payload* as JSON and return the response status.

    Raises:
        RuntimeError: for a non-2xx response, quoting the start of its body.
        aiohttp.ClientError, TimeoutError: when the request itself fails.
    """
    connector = aiohttp.TCPConnector(ssl=None if settings.ssl_verify else False)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), connector=connector) as session:
        async with session.post(url, json=payload, headers=headers or {}) as response:
            if not 200 <= response.status < 300:
                body = (await response.text())[:_BODY_QUOTE]
                raise RuntimeError(f"webhook returned HTTP {response.status}: {body}")
            logger.debug("webhook accepted notification (HTTP %d)", response.status)
            return response.status
