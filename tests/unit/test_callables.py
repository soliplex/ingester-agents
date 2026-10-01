"""Tests for the shared manifest-hook plumbing -- 100% branch coverage required."""

import asyncio
import enum
import os
import threading
from typing import NamedTuple

import pytest

from soliplex.agents.manifest import callables


class Status(enum.StrEnum):
    CONTINUE = "continue"
    MODIFIED = "modified"
    SKIP = "skip"


class Result(NamedTuple):
    status: Status
    message: str | None = None
    data: bytes | None = None


ALL = set(Status)


# --- resolve_method / accepts_kwarg / inject ---


def test_resolve_method_colon():
    assert callables.resolve_method("os:getcwd") is os.getcwd


def test_resolve_method_dotted():
    assert callables.resolve_method("os.getcwd") is os.getcwd


def test_resolve_method_missing_attribute():
    with pytest.raises(AttributeError):
        callables.resolve_method("os:no_such_function")


def test_accepts_kwarg_named_var_keyword_and_absent():
    def named(x, *, config=None): ...

    def var(x, **kwargs): ...

    def neither(x, *, y=1): ...

    assert callables.accepts_kwarg(named, "config") is True
    assert callables.accepts_kwarg(var, "anything") is True
    assert callables.accepts_kwarg(neither, "config") is False


def test_inject_respects_explicit_kwargs_and_signature():
    def method(x, *, context=None, other=None): ...

    merged = callables.inject(method, {"other": 1, "context": "mine"}, {"context": "runner", "unused": 2})
    assert merged == {"other": 1, "context": "mine"}
    assert callables.inject(method, {}, {"context": "runner", "unused": 2}) == {"context": "runner"}


# --- invoke ---


@pytest.mark.asyncio
async def test_invoke_async_is_awaited():
    async def method(a, *, b):
        return a + b

    assert await callables.invoke(method, 1, kwargs={"b": 2}) == 3


@pytest.mark.asyncio
async def test_invoke_sync_runs_in_a_thread():
    main = threading.get_ident()

    def method():
        return threading.get_ident()

    assert await callables.invoke(method) != main


@pytest.mark.asyncio
async def test_invoke_sync_inline_when_asked():
    main = threading.get_ident()

    def method():
        return threading.get_ident()

    assert await callables.invoke(method, in_thread=False) == main


@pytest.mark.asyncio
async def test_invoke_sync_returning_awaitable_is_awaited():
    async def inner():
        return "done"

    def method():
        return inner()

    assert await callables.invoke(method, in_thread=False) == "done"


@pytest.mark.asyncio
async def test_invoke_timeout_cancels_an_async_step():
    cancelled = asyncio.Event()

    async def method():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        await callables.invoke(method, timeout=0.01)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_invoke_within_timeout():
    async def method():
        return 1

    assert await callables.invoke(method, timeout=5) == 1


# --- clean_message ---


def test_clean_message_none_blank_strip_and_cap():
    assert callables.clean_message(None) is None
    assert callables.clean_message("   ") is None
    assert callables.clean_message("  hi \n") == "hi"
    long = callables.clean_message("x" * 5000)
    assert len(long) == callables.MESSAGE_LIMIT
    assert long.endswith("…")


def test_clean_message_rejects_non_string():
    with pytest.raises(TypeError, match="str or None"):
        callables.clean_message(42)


# --- normalize ---


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, Result(Status.CONTINUE)),
        (Status.SKIP, Result(Status.SKIP)),
        ("skip", Result(Status.SKIP)),
        ((Status.SKIP, "password protected"), Result(Status.SKIP, "password protected")),
        ((Status.SKIP, None), Result(Status.SKIP)),
        (("modified", "m", b"x"), Result(Status.MODIFIED, "m", b"x")),
        (Result(Status.CONTINUE, "  note "), Result(Status.CONTINUE, "note")),
    ],
)
def test_normalize_accepted_forms(value, expected):
    assert callables.normalize(value, Result, Status, ALL) == expected


def test_normalize_unknown_status():
    with pytest.raises(ValueError, match="'bogus'"):
        callables.normalize("bogus", Result, Status, ALL)


def test_normalize_disallowed_status():
    with pytest.raises(ValueError, match="not allowed here"):
        callables.normalize(Status.MODIFIED, Result, Status, {Status.CONTINUE, Status.SKIP})


@pytest.mark.parametrize("value", [(), ("skip", "a", b"b", "too many")])
def test_normalize_wrong_arity(value):
    with pytest.raises(ValueError, match="1 to 3 values"):
        callables.normalize(value, Result, Status, ALL)


def test_normalize_wrong_type():
    with pytest.raises(TypeError, match="unsupported step return type int"):
        callables.normalize(7, Result, Status, ALL)


def test_normalize_non_string_message():
    with pytest.raises(TypeError):
        callables.normalize(("skip", 5), Result, Status, ALL)


# --- describe_error ---


def test_describe_error_with_and_without_message():
    assert callables.describe_error(ValueError("bad")) == "ValueError: bad"
    assert callables.describe_error(TimeoutError()) == "TimeoutError"
