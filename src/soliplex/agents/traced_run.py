"""Run a console script in-process, as a child of a trace passed in the environment.

Usage::

    python -m soliplex.agents.traced_run <console-script> [args...]

OpenTelemetry does not read ``TRACEPARENT`` from the environment, and neither
does Logfire, so a subprocess that should join its parent's trace has to
attach the context itself. This module does it for a CLI that can't: it reads
``TRACEPARENT`` / ``TRACESTATE``, attaches that context for the life of the
process, and then calls the script's own entry point with the original
arguments. Anything the CLI traces afterwards -- including under
``asyncio.run``, which inherits the context current when it starts --
becomes a child of the parent's span.

:mod:`soliplex.agents.manifest.haiku_process` runs haiku commands through it
when ``HAIKU_TRACE_WRAPPER`` is on. It's redundant once haiku-rag reads
``TRACEPARENT`` itself, and harmless alongside it: both attach the same
context.

It only works for a console script installed in the same environment as
this package, which it looks up by name among the ``console_scripts`` entry
points.
"""

import ntpath
import os
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points

from opentelemetry import context
from opentelemetry import propagate
from opentelemetry import trace

USAGE = "usage: python -m soliplex.agents.traced_run <console-script> [args...]"


def script_name(command: str) -> str:
    """The console-script name *command* invokes (``/venv/bin/haiku-ingester`` -> ``haiku-ingester``).

    ``ntpath`` rather than ``pathlib``: it splits on both ``/`` and ``\\`` on
    every platform, so a Windows path gives the same name when read on Linux,
    where ``Path`` treats a backslash as part of the filename.
    """
    name = ntpath.basename(command)
    return name[:-4] if name.lower().endswith(".exe") else name


def find_entry_point(name: str) -> EntryPoint | None:
    """The ``console_scripts`` entry point called *name*, if one is installed."""
    return next(iter(entry_points(group="console_scripts", name=name)), None)


def attach_parent(environ: Mapping[str, str] = os.environ) -> bool:
    """Attach the trace context in *environ* for the rest of the process.

    Never detached: the context is meant to last as long as the process.

    Returns:
        Whether a valid context was found and attached. A missing or malformed
        ``TRACEPARENT`` attaches nothing.
    """
    carrier = {key.lower(): environ[key] for key in ("TRACEPARENT", "TRACESTATE") if environ.get(key)}
    if "traceparent" not in carrier:
        return False
    parent = propagate.extract(carrier)
    if not trace.get_current_span(parent).get_span_context().is_valid:
        return False
    context.attach(parent)
    return True


def main(argv: Sequence[str] | None = None):
    """Attach the parent context, then hand over to the console script.

    Returns:
        Whatever the entry point returns (its exit status), ``2`` without a
        command, or ``127`` when the command is not a console script here.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    entry_point = find_entry_point(script_name(argv[0]))
    if entry_point is None:
        print(f"traced_run: {argv[0]} is not a console script in this environment", file=sys.stderr)
        return 127
    attach_parent()
    # The script sees exactly the command line it would have run with.
    sys.argv = argv
    return entry_point.load()()


if __name__ == "__main__":  # pragma: no cover - exercised through a real subprocess
    sys.exit(main())
