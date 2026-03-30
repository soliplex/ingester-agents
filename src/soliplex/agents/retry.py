"""Shared retry utilities for ingester-agents.

Centralises retry primitives so every HTTP-calling module shares
the same tenacity-based behaviour, including Retry-After support.
"""

import datetime
import logging
from email.utils import parsedate_to_datetime

import aiohttp
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

logger = logging.getLogger(__name__)

# Max seconds we are willing to honour from a Retry-After header.
DEFAULT_RETRY_AFTER_CAP = 300


class RetryableHTTPError(Exception):
    """Raised on HTTP status codes that should trigger a retry."""

    def __init__(
        self,
        status: int,
        retry_after: float | None = None,
        body: str = "",
    ) -> None:
        self.status = status
        self.retry_after = retry_after
        self.body = body
        super().__init__(f"HTTP {status}")


# Status codes that should be retried automatically.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 509}

# Exception types that trigger a tenacity retry.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RetryableHTTPError,
    TimeoutError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    ConnectionResetError,
)


def parse_retry_after(
    headers: dict | aiohttp.typedefs.CIMultiDictProxy,
    cap: float = DEFAULT_RETRY_AFTER_CAP,
) -> float | None:
    """Parse the ``Retry-After`` header value.

    Supports both integer-seconds and RFC 7231 HTTP-date formats.
    Returns *None* when the header is absent or unparseable, and
    clamps the result to *cap* seconds.
    """
    raw = headers.get("Retry-After")
    if raw is None:
        return None

    raw = str(raw).strip()

    # Try integer seconds first.
    try:
        value = float(raw)
        return min(value, cap)
    except ValueError:
        pass

    # Try HTTP-date (RFC 7231).
    try:
        target = parsedate_to_datetime(raw)
        delta = (target - datetime.datetime.now(datetime.UTC)).total_seconds()
        return min(max(delta, 0), cap)
    except Exception:  # noqa: BLE001
        logger.debug("Unparseable Retry-After value: %s", raw)
        return None


class WaitWithRetryAfter(wait_exponential):
    """Exponential back-off that honours ``retry_after`` on exceptions.

    If the exception being retried carries a ``retry_after`` attribute
    (float seconds), the wait time is the greater of the exponential
    back-off and the ``retry_after`` value.
    """

    def __call__(self, retry_state):
        exp_wait = super().__call__(retry_state)
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            return max(float(retry_after), exp_wait)
        return exp_wait


def retry_policy(
    max_attempts: int = 5,
    max_delay: float = 120,
) -> dict:
    """Return a dict of tenacity kwargs for ``AsyncRetrying``.

    Usage::

        async for attempt in AsyncRetrying(**retry_policy()):
            with attempt:
                ...
    """
    return {
        "retry": retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        "wait": WaitWithRetryAfter(multiplier=1, max=max_delay),
        "stop": stop_after_attempt(max_attempts),
        "reraise": True,
    }
