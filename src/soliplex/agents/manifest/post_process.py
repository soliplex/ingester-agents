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
import os
from collections.abc import Callable
from contextlib import contextmanager
from inspect import Parameter

from soliplex.agents.config import Manifest
from soliplex.agents.config import settings
from soliplex.agents.local_store import sanitize_source
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


def _accepts_kwarg(method: Callable, name: str) -> bool:
    """Whether ``method`` accepts ``name`` as a keyword (named or via ``**kwargs``)."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return False
    if name in params:
        return True
    return any(p.kind is Parameter.VAR_KEYWORD for p in params.values())


@contextmanager
def _load_env(manifest: Manifest):
    """Temporarily expose the env vars ``run_load`` injects into the load
    subprocess (``SOURCE`` / ``DOWNLOAD_DIR``).

    In-process callbacks load the same haiku config the load used, and that
    config interpolates ``${SOURCE}`` / ``${DOWNLOAD_DIR}`` (which
    ``load_yaml_config`` expands eagerly). Those two are only ever set in the
    subprocess env, so mirror them here for the duration of the callbacks. Other
    ``${VAR}`` references (``STATE_DIR``, embedder URLs, ...) are expected in the
    inherited environment, exactly as they are for the subprocess. Loads are
    serialized, so the temporary global mutation does not race.
    """
    overrides = {
        "SOURCE": sanitize_source(manifest.source),
        "DOWNLOAD_DIR": settings.download_dir,
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def run_post_process(
    manifest: Manifest,
    *,
    ingester_exit_code: int | None = None,
) -> list[dict]:
    """Run ``manifest.config.post_process`` callbacks in order.

    ``ingester_exit_code`` is the haiku-ingester load's exit code (``None`` on
    timeout). Callbacks run regardless of it, so a step can inspect the outcome;
    it is auto-injected as an ``ingester_exit_code`` kwarg for callables that
    accept one.

    Runs the steps in order and **terminates on the first error**: a step that
    raises is logged and the exception propagates, so the remaining steps do not
    run. Returns a per-step ``{"method", "ok", "error"}`` list (all ``ok``) only
    when every step succeeds.
    """
    if manifest.config is None or not manifest.config.post_process:
        return []

    results: list[dict] = []
    with _load_env(manifest):
        for step in manifest.config.post_process:
            logger.info(
                "Running post-process '%s' for source '%s'",
                step.method,
                manifest.source,
            )
            try:
                method = _resolve_method(step.method)
                kwargs = dict(step.kwargs)
                if "config" not in kwargs and _accepts_kwarg(method, "config"):
                    kwargs["config"] = resolve_haiku_cfg(manifest)
                if "ingester_exit_code" not in kwargs and _accepts_kwarg(method, "ingester_exit_code"):
                    kwargs["ingester_exit_code"] = ingester_exit_code
                value = method(manifest.source, **kwargs)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                logger.exception(
                    "Post-process '%s' failed for source '%s'; terminating",
                    step.method,
                    manifest.source,
                )
                raise
            logger.info("Post-process '%s' completed", step.method)
            results.append({"method": step.method, "ok": True, "error": None})
    return results
