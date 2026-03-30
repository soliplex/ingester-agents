"""Tests for the shared retry utilities module."""

import datetime
from unittest.mock import MagicMock

from tenacity import RetryCallState
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt

from soliplex.agents.retry import DEFAULT_RETRY_AFTER_CAP
from soliplex.agents.retry import RETRYABLE_EXCEPTIONS
from soliplex.agents.retry import RETRYABLE_STATUS_CODES
from soliplex.agents.retry import RetryableHTTPError
from soliplex.agents.retry import WaitWithRetryAfter
from soliplex.agents.retry import parse_retry_after
from soliplex.agents.retry import retry_policy


class TestRetryableHTTPError:
    def test_basic(self):
        err = RetryableHTTPError(429)
        assert err.status == 429
        assert err.retry_after is None
        assert err.body == ""
        assert "429" in str(err)

    def test_with_retry_after(self):
        err = RetryableHTTPError(429, retry_after=30.0, body="slow down")
        assert err.status == 429
        assert err.retry_after == 30.0
        assert err.body == "slow down"

    def test_is_exception(self):
        assert issubclass(RetryableHTTPError, Exception)


class TestRetryableStatusCodes:
    def test_contains_expected(self):
        for code in (429, 500, 502, 503, 504, 509):
            assert code in RETRYABLE_STATUS_CODES


class TestRetryableExceptions:
    def test_contains_expected_types(self):
        assert RetryableHTTPError in RETRYABLE_EXCEPTIONS
        assert TimeoutError in RETRYABLE_EXCEPTIONS
        assert ConnectionResetError in RETRYABLE_EXCEPTIONS


class TestParseRetryAfter:
    def test_integer_seconds(self):
        assert parse_retry_after({"Retry-After": "30"}) == 30.0

    def test_float_seconds(self):
        assert parse_retry_after({"Retry-After": "1.5"}) == 1.5

    def test_missing_header(self):
        assert parse_retry_after({}) is None

    def test_none_value(self):
        assert parse_retry_after({"Retry-After": None}) is None

    def test_unparseable_value(self):
        assert parse_retry_after({"Retry-After": "not-a-number-or-date"}) is None

    def test_clamped_to_cap(self):
        result = parse_retry_after({"Retry-After": "9999"})
        assert result == DEFAULT_RETRY_AFTER_CAP

    def test_custom_cap(self):
        result = parse_retry_after({"Retry-After": "100"}, cap=50)
        assert result == 50

    def test_http_date_format(self):
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=10)
        date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        result = parse_retry_after({"Retry-After": date_str})
        assert result is not None
        assert 0 < result <= 11  # Allow 1s tolerance

    def test_http_date_in_past(self):
        past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=10)
        date_str = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        result = parse_retry_after({"Retry-After": date_str})
        assert result == 0  # max(delta, 0) where delta < 0

    def test_http_date_clamped(self):
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=999)
        date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        result = parse_retry_after({"Retry-After": date_str}, cap=60)
        assert result == 60


class TestWaitWithRetryAfter:
    def _make_retry_state(self, exception=None):
        rs = MagicMock(spec=RetryCallState)
        rs.attempt_number = 1
        if exception is not None:
            outcome = MagicMock()
            outcome.exception.return_value = exception
            rs.outcome = outcome
        else:
            rs.outcome = None
        return rs

    def test_falls_back_to_exponential(self):
        wait = WaitWithRetryAfter(multiplier=1, max=30)
        rs = self._make_retry_state(exception=TimeoutError())
        result = wait(rs)
        assert isinstance(result, (int, float))
        assert result >= 0

    def test_uses_retry_after_when_larger(self):
        wait = WaitWithRetryAfter(multiplier=1, max=30)
        exc = RetryableHTTPError(429, retry_after=60.0)
        rs = self._make_retry_state(exception=exc)
        result = wait(rs)
        assert result >= 60.0

    def test_uses_exponential_when_larger_than_retry_after(self):
        wait = WaitWithRetryAfter(multiplier=1, max=120)
        exc = RetryableHTTPError(429, retry_after=0.001)
        rs = self._make_retry_state(exception=exc)
        result = wait(rs)
        # Exponential for attempt 1 with multiplier=1 is 2
        assert result >= 0.001

    def test_no_outcome(self):
        wait = WaitWithRetryAfter(multiplier=1, max=30)
        rs = self._make_retry_state(exception=None)
        result = wait(rs)
        assert isinstance(result, (int, float))

    def test_exception_without_retry_after(self):
        wait = WaitWithRetryAfter(multiplier=1, max=30)
        exc = ConnectionResetError("reset")
        rs = self._make_retry_state(exception=exc)
        result = wait(rs)
        assert isinstance(result, (int, float))


class TestRetryPolicy:
    def test_default_values(self):
        policy = retry_policy()
        assert "retry" in policy
        assert "wait" in policy
        assert "stop" in policy
        assert policy["reraise"] is True

    def test_custom_values(self):
        policy = retry_policy(max_attempts=3, max_delay=30)
        assert isinstance(policy["stop"], stop_after_attempt)
        assert isinstance(policy["wait"], WaitWithRetryAfter)

    def test_wait_is_wait_with_retry_after(self):
        policy = retry_policy()
        assert isinstance(policy["wait"], WaitWithRetryAfter)

    def test_retry_targets_retryable_exceptions(self):
        policy = retry_policy()
        assert isinstance(policy["retry"], retry_if_exception_type)
