"""Invoke a manifest's ``config.post_process`` callbacks after a load.

Each step names a dotted-path callable (``pkg.mod:func`` or ``pkg.mod.func``)
that is invoked as ``method(source, **kwargs)`` -- ``source`` is the manifest's
source, ``kwargs`` are the configured extra args. Steps run **in order** after
``haiku-ingester`` finishes (see :func:`haiku_loader.run_load`).

Two conveniences:

* **config auto-inject** -- when a step omits ``config`` and the callable
  accepts one (an explicit ``config`` parameter or ``**kwargs``), the manifest's
  resolved haiku config path is passed so the callback opens the store with the
  same config the load used;
* **log-and-continue** -- a failing step is logged and recorded, and the
  remaining steps still run.

See ``docs/post-process-plan.md``.
"""

import importlib
import inspect
import logging
from collections.abc import Callable
from inspect import Parameter

from soliplex.agents.config import Manifest
from soliplex.agents.manifest.haiku_loader import resolve_haiku_cfg

logger = logging.getLogger(__name__)


def _resolve_method(spec: str) -> Callable:
    """Import a dotted-path callable.

    Accepts ``"pkg.mod:func"`` (module / attribute split on ``:``) and, as a
    fallback, ``"pkg.mod.func"`` (split on the last ``.``).
    """
    module_name, sep, attr = spec.partition(":")
    if not sep:
        module_name, _, attr = spec.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _wants_config(method: Callable) -> bool:
    """Whether ``method`` accepts a ``config`` keyword (named or via ``**kwargs``)."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False
    if "config" in params:
        return True
    return any(p.kind is Parameter.VAR_KEYWORD for p in params.values())


async def run_post_process(manifest: Manifest) -> list[dict]:
    """Run ``manifest.config.post_process`` callbacks in order.

    Returns a per-step outcome list of ``{"method", "ok", "error"}`` dicts. A
    step that raises is logged and recorded with ``ok=False``; execution
    continues with the next step.
    """
    if manifest.config is None or not manifest.config.post_process:
        return []

    results: list[dict] = []
    for step in manifest.config.post_process:
        outcome: dict = {"method": step.method, "ok": True, "error": None}
        try:
            method = _resolve_method(step.method)
            kwargs = dict(step.kwargs)
            if "config" not in kwargs and _wants_config(method):
                kwargs["config"] = resolve_haiku_cfg(manifest)
            logger.info(
                "Running post-process '%s' for source '%s'",
                step.method,
                manifest.source,
            )
            value = method(manifest.source, **kwargs)
            if inspect.isawaitable(value):
                await value
            logger.info("Post-process '%s' completed", step.method)
        except Exception as exc:
            logger.exception(
                "Post-process '%s' failed for source '%s'",
                step.method,
                manifest.source,
            )
            outcome["ok"] = False
            outcome["error"] = str(exc)
        results.append(outcome)
    return results
