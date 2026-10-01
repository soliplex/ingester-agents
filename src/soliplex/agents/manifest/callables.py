"""Shared plumbing for the manifest hooks (``pre_run``, ``pre_process``, ``post_process``).

Every hook names its steps the same way -- a dotted import path plus keyword
arguments -- and fills in values the step did not set but can accept. This
module holds that common part, so the three runners only own what is
genuinely theirs: when they fire and what a step's answer means.
"""

import asyncio
import enum
import importlib
import inspect
from collections.abc import Callable
from collections.abc import Iterable
from inspect import Parameter
from typing import Any

# Longest step message kept; anything longer is cut and marked, so an exception
# dump cannot bloat the audit tables or the log line.
MESSAGE_LIMIT = 1000


def resolve_method(spec: str) -> Callable:
    """Import a dotted-path callable.

    Accepts ``"pkg.mod:func"`` (module / attribute split on ``:``) and, as a
    fallback, ``"pkg.mod.func"`` (split on the last ``.``).
    """
    module_name, sep, attr = spec.partition(":")
    if not sep:
        module_name, _, attr = spec.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def accepts_kwarg(method: Callable, name: str) -> bool:
    """Whether ``method`` accepts ``name`` as a keyword (named or via ``**kwargs``)."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False
    if name in params:
        return True
    return any(p.kind is Parameter.VAR_KEYWORD for p in params.values())


def inject(method: Callable, kwargs: dict[str, Any], available: dict[str, Any]) -> dict[str, Any]:
    """*kwargs* plus each *available* value the step did not set and *method* accepts.

    An explicitly configured kwarg always wins, so a manifest can override what
    the runner would have supplied.
    """
    merged = dict(kwargs)
    for name, value in available.items():
        if name not in merged and accepts_kwarg(method, name):
            merged[name] = value
    return merged


async def invoke(
    method: Callable,
    *args: Any,
    kwargs: dict[str, Any] | None = None,
    timeout: float | None = None,
    in_thread: bool = True,
) -> Any:
    """Call *method*, awaiting it when it is async.

    A sync callable runs in a worker thread when *in_thread* is set, so a
    CPU-bound step (pdfium, a compression pass) does not stall the event loop.
    *timeout* bounds the call: an async step is cancelled, but a sync step in a
    thread cannot be interrupted -- the wait is abandoned while the thread runs
    on to completion.

    Raises:
        TimeoutError: when *timeout* elapses first.
    """
    kwargs = kwargs or {}

    async def _call() -> Any:
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        if in_thread:
            value = await asyncio.to_thread(method, *args, **kwargs)
        else:
            value = method(*args, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return value

    if timeout is None:
        return await _call()
    return await asyncio.wait_for(_call(), timeout)


def clean_message(message: Any) -> str | None:
    """A step's message, stripped and capped; blank becomes ``None``.

    Raises:
        TypeError: when *message* is neither a string nor ``None``.
    """
    if message is None:
        return None
    if not isinstance(message, str):
        raise TypeError(f"step message must be a str or None, not {type(message).__name__}")
    message = message.strip()
    if not message:
        return None
    if len(message) > MESSAGE_LIMIT:
        message = message[: MESSAGE_LIMIT - 1] + "…"
    return message


def normalize(value: Any, result_type: type, status_type: type[enum.Enum], allowed: Iterable) -> Any:
    """Turn any accepted step return into a *result_type* instance.

    Accepted forms: ``None`` (the status type's ``CONTINUE``), a bare status or
    its string value, a tuple of one up to ``len(result_type._fields)`` items
    (status first, message second), or a *result_type* itself. The status must
    be one of *allowed*, and the message is passed through
    :func:`clean_message`.

    Raises:
        TypeError: for an unsupported type, or a message that is not a string.
        ValueError: for an unknown or disallowed status, or a wrong arity.
    """
    if value is None:
        fields: tuple = (status_type["CONTINUE"],)
    elif isinstance(value, str):
        fields = (value,)
    elif isinstance(value, tuple):
        fields = tuple(value)
    else:
        raise TypeError(f"unsupported step return type {type(value).__name__}")
    if not 1 <= len(fields) <= len(result_type._fields):
        raise ValueError(f"a step may return 1 to {len(result_type._fields)} values, not {len(fields)}")
    status = status_type(fields[0])
    allowed = set(allowed)
    if status not in allowed:
        names = ", ".join(sorted(s.value for s in allowed))
        raise ValueError(f"status '{status.value}' is not allowed here (expected one of: {names})")
    message = clean_message(fields[1]) if len(fields) > 1 else None
    return result_type(status, message, *fields[2:])


def describe_error(exc: BaseException) -> str:
    """``"<ExcType>: <message>"`` for an audit row, or just the type when the message is empty."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
