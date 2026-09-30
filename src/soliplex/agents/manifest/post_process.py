"""Invoke a manifest's ``config.post_process`` callbacks after a load.

Each step names a dotted-path callable (``pkg.mod:func`` or ``pkg.mod.func``)
that is invoked as ``method(source, **kwargs)`` -- ``source`` is the manifest's
source, ``kwargs`` are the configured extra args. Steps run **in order** after
``haiku-ingester`` finishes (see :func:`haiku_loader.run_load`).

Three things a step does not have to arrange for itself:

* **config auto-inject** -- when a step omits ``config`` and the callable
  accepts one (an explicit ``config`` parameter or ``**kwargs``), the manifest's
  resolved haiku config path is passed so the callback opens the store with the
  same config the load used;
* **context auto-inject** -- likewise for ``context``, which receives the
  :class:`~soliplex.agents.manifest.context.LoadContext` for the source: the
  resolved download target, store, and sidecar facade. A callback that needs
  storage does not have to rediscover it from the environment;
* **outcome auto-inject** -- likewise for ``ingester_exit_code``, the load's
  exit code (``None`` on timeout). Callbacks fire whatever the load did, so a
  step that only makes sense after a clean load has to be able to ask.

A step that raises stops the chain: the exception is logged and propagates, and
the steps after it do not run. See :func:`run_post_process`, and the README's
"Post-process callbacks" for the configuration side.
"""

import importlib
import inspect
import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from inspect import Parameter

from soliplex.agents import telemetry
from soliplex.agents.config import Manifest
from soliplex.agents.manifest.context import LoadContext
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
def _load_env(manifest: Manifest, context: LoadContext):
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
    overrides = {key: context.env({})[key] for key in ("SOURCE", "DOWNLOAD_DIR", "DOWNLOAD_URI")}
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
    context = LoadContext.for_source(manifest.source)
    with _load_env(manifest, context):
        for index, step in enumerate(manifest.config.post_process):
            attributes = {
                telemetry.POST_PROCESS_METHOD: step.method,
                telemetry.POST_PROCESS_INDEX: index,
                telemetry.MANIFEST_ID: manifest.id,
                telemetry.MANIFEST_SOURCE: manifest.source,
                telemetry.INGESTER_EXIT_CODE: ingester_exit_code,
            }
            # A step that raises leaves the span with the exception, which marks
            # it as failed; no later step gets a span, since none runs.
            with telemetry.span("post-process", f"post-process {step.method}", attributes):
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
                    if "context" not in kwargs and _accepts_kwarg(method, "context"):
                        kwargs["context"] = context
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
