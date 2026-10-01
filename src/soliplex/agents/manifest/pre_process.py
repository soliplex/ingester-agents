"""Run a manifest's ``config.pre_process`` steps on each document before it is stored.

Pre-processing happens inside :func:`~soliplex.agents.local_store.write_document`
-- the one call every agent makes to store a document -- so it covers every
component type without any agent knowing about it, and only ever sees
documents that are new or changed upstream (unchanged ones are never fetched).

:func:`~soliplex.agents.manifest.runner.run_manifest` installs a
:class:`PreProcessRun` for the duration of the run (:func:`activate`);
``write_document`` asks for it with :func:`current`. Outside a manifest run
there is none, and documents are written exactly as before.

For each document with at least one matching step, the bytes are spooled to a
private temp directory (``settings.pre_process_spool_dir``), and each step is
called as ``method(document, **kwargs)`` with a :class:`PreProcessDocument`
pointing at the spooled file. A step answers with a
:class:`PreProcessStatus` (or a tuple / :class:`PreProcessResult` carrying a
message, see :func:`~soliplex.agents.manifest.callables.normalize`):

* ``CONTINUE`` -- nothing to do;
* ``MODIFIED`` -- here is new content (``data`` or ``path``); later steps see it
  and it is what gets stored;
* ``SKIP`` -- do not store this document. Nothing is written, any previously
  stored version is removed, and the remaining steps do not run.

The agent still records the document's state row afterwards, so a skipped
document is not fetched again until it changes upstream; ``si-agent manifest
reprocess`` forgets it to force a retry. Every write is audited in the state
DB (see :mod:`soliplex.agents.local_state`).

Steps run one at a time per run, even when an agent writes concurrently:
pdfium is not thread-safe, and serializing also bounds the spool to roughly
one document.
"""

import asyncio
import contextvars
import datetime
import enum
import hashlib
import logging
import tempfile
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import NamedTuple

from soliplex.agents import local_state
from soliplex.agents.config import Manifest
from soliplex.agents.config import PreProcessStep
from soliplex.agents.config import settings
from soliplex.agents.manifest import callables

logger = logging.getLogger(__name__)

# Audit status for a step that raised under ``on_error: continue`` (and for a
# document one such step ran on). Not a status a step can return.
STATUS_ERROR = "error"


class PreProcessStatus(enum.StrEnum):
    """What a pre-process step decided about a document."""

    CONTINUE = "continue"
    MODIFIED = "modified"
    SKIP = "skip"


class PreProcessResult(NamedTuple):
    """A step's full answer. A plain ``(status, message)`` tuple is the same thing.

    ``MODIFIED`` needs exactly one of ``data`` (the new content) or ``path``
    (a file the step wrote, normally under ``document.workdir``; a relative
    path is taken relative to it). ``metadata`` is merged into the document's
    ``.meta.json`` under ``metadata["pre_process"][<method>]``.
    """

    status: PreProcessStatus
    message: str | None = None
    data: bytes | None = None
    path: Path | str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PreProcessDocument:
    """The document a step is asked about.

    ``path`` is a private, spooled copy of the content as it stands -- after
    any earlier step's modification. Treat it as read-only; write any output
    under ``workdir``, which is this step's own scratch directory. ``sha256``
    is the hash of ``path``.
    """

    source: str
    uri: str
    key: str
    mime_type: str | None
    path: Path
    workdir: Path
    sha256: str

    def read_bytes(self) -> bytes:
        """The document's content, for steps that would rather not open the file."""
        return self.path.read_bytes()


@dataclass(frozen=True)
class ResolvedStep:
    """A configured step with its method already imported."""

    index: int
    step: PreProcessStep
    method: Callable

    def matches(self, mime_type: str | None) -> bool:
        """Whether this step applies to a document of *mime_type*."""
        if self.step.mime_types is None:
            return True
        if mime_type is None:
            return False
        return mime_type.lower() in {m.lower() for m in self.step.mime_types}


@dataclass
class Outcome:
    """What pre-processing decided for one document.

    ``status`` is the document-level outcome: ``skip`` beats ``error`` (a step
    raised under ``on_error: continue``), which beats ``modified``, which
    beats ``continue``. ``data`` is the content to store, ``None`` when
    skipped.
    """

    status: str
    data: bytes | None
    method: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def skipped(self) -> bool:
        return self.status == PreProcessStatus.SKIP


class PreProcessFailed(RuntimeError):
    """A step failed under ``on_error: fail``; the document is not stored."""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=False).hexdigest()


def resolve_steps(manifest: Manifest) -> list[ResolvedStep]:
    """Import every pre-process step for *manifest*, so a bad path fails before the run.

    Only the steps the manifest lists run: no ``config.pre_process`` means no
    pre-processing.

    Raises:
        ImportError, AttributeError: when a step's method cannot be imported.
    """
    steps = manifest.config.pre_process if manifest.config else []
    return [ResolvedStep(index, step, callables.resolve_method(step.method)) for index, step in enumerate(steps)]


@dataclass
class PreProcessRun:
    """The pre-process state of one manifest run: its steps and what they decided."""

    manifest_id: str
    source: str
    steps: list[ResolvedStep]
    # LoadContext for the source, injected as ``context`` into steps that ask.
    context: Any = None
    checked: int = 0
    modified: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    _lock: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1), repr=False)

    def matching(self, mime_type: str | None) -> list[ResolvedStep]:
        """The steps that apply to a document of *mime_type*, in order."""
        return [step for step in self.steps if step.matches(mime_type)]

    def report(self) -> dict:
        """This run's outcomes, for the manifest result."""
        return {
            "checked": self.checked,
            "modified": list(self.modified),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }

    async def process(self, *, source: str, uri: str, key: str, mime_type: str | None, data: bytes) -> Outcome:
        """Run the matching steps over *data* and decide what to store.

        Raises:
            PreProcessFailed: when a step fails under ``on_error: fail``.
        """
        steps = self.matching(mime_type)
        async with self._lock:
            self.checked += 1
            run_at = _utcnow()
            with tempfile.TemporaryDirectory(
                prefix="pre-process-", dir=settings.pre_process_spool_dir, ignore_cleanup_errors=True
            ) as tmp:
                return await self._process(source, uri, key, mime_type, data, steps, Path(tmp), run_at)

    async def _process(
        self,
        source: str,
        uri: str,
        key: str,
        mime_type: str | None,
        data: bytes,
        steps: list[ResolvedStep],
        spool: Path,
        run_at: str,
    ) -> Outcome:
        name = Path(key).name or "document"
        current = spool / "input" / name
        current.parent.mkdir()
        current.write_bytes(data)
        original_sha = current_sha = _sha256(data)

        rows: list[dict] = []
        metadata: dict[str, Any] = {}
        skip: tuple[str, str | None] | None = None
        first_error: tuple[str, str | None] | None = None
        last_modified: tuple[str, str | None] | None = None

        for resolved in steps:
            method_name = resolved.step.method
            workdir = spool / f"step-{resolved.index}"
            workdir.mkdir()
            input_sha = current_sha
            document = PreProcessDocument(
                source=source,
                uri=uri,
                key=key,
                mime_type=mime_type,
                path=current,
                workdir=workdir,
                sha256=current_sha,
            )
            try:
                kwargs = callables.inject(resolved.method, resolved.step.kwargs, {"context": self.context})
                value = await callables.invoke(resolved.method, document, kwargs=kwargs)
                result, output = _interpret(value, workdir, name)
                status: str = result.status
                message = result.message
                if output is not None:
                    output_sha = _sha256(output.read_bytes())
                    if output_sha == current_sha:
                        logger.debug("pre-process %s left %s unchanged; treating as continue", method_name, uri)
                        status = PreProcessStatus.CONTINUE
                    else:
                        current, current_sha = output, output_sha
                        last_modified = (method_name, message)
                if result.metadata:
                    metadata[method_name] = result.metadata
            except Exception as e:
                message = callables.describe_error(e)
                if resolved.step.on_error == "fail":
                    rows.append(_step_row(resolved, STATUS_ERROR, message, input_sha, input_sha, run_at))
                    self.errors.append({"uri": uri, "method": method_name, "message": message})
                    self._record(source, uri, original_sha, None, STATUS_ERROR, method_name, message, rows, run_at)
                    logger.exception("pre-process %s failed on %s; document not stored", method_name, uri)
                    raise PreProcessFailed(f"pre-process {method_name} failed on {uri}: {message}") from e
                logger.warning("pre-process %s failed on %s: %s", method_name, uri, message, exc_info=True)
                if resolved.step.on_error == "skip":
                    status = PreProcessStatus.SKIP
                else:
                    status = STATUS_ERROR
                    if first_error is None:
                        first_error = (method_name, message)
                    self.errors.append({"uri": uri, "method": method_name, "message": message})
            rows.append(_step_row(resolved, status, message, input_sha, current_sha, run_at))
            if status == PreProcessStatus.SKIP:
                skip = (method_name, message)
                break

        previous = local_state.get_pre_process_document(source, uri)
        changed = previous is not None and previous["input_sha256"] != original_sha
        if previous is not None and not changed:
            logger.debug("pre-process: %s re-fetched with unchanged content", uri)
        elif changed:
            logger.info("content of %s changed (%s -> %s)", uri, previous["input_sha256"][:12], original_sha[:12])

        if skip is not None:
            method_name, message = skip
            self._record(source, uri, original_sha, None, PreProcessStatus.SKIP, method_name, message, rows, run_at)
            self.skipped.append({"uri": uri, "method": method_name, "message": message})
            already_skipped = previous is not None and previous["status"] == PreProcessStatus.SKIP
            if already_skipped and not changed:
                logger.debug("pre-process %s skipped %s again%s", method_name, uri, _suffix(message))
            elif already_skipped:
                logger.info("new version of %s still skipped%s", uri, _suffix(message))
            else:
                logger.info("pre-process %s skipped %s%s", method_name, uri, _suffix(message))
            return Outcome(PreProcessStatus.SKIP, None, method_name, message, metadata)

        modified = current_sha != original_sha
        final = current.read_bytes() if modified else data
        if first_error is not None:
            status, (method_name, message) = STATUS_ERROR, first_error
        elif modified:
            status, (method_name, message) = PreProcessStatus.MODIFIED, last_modified
        else:
            status, method_name, message = PreProcessStatus.CONTINUE, None, None
        if modified:
            mod_method, mod_message = last_modified
            self.modified.append({"uri": uri, "method": mod_method, "message": mod_message})
            logger.info("pre-process %s modified %s%s", mod_method, uri, _suffix(mod_message))
        self._record(source, uri, original_sha, current_sha, status, method_name, message, rows, run_at)
        return Outcome(status, final, method_name, message, metadata)

    def _record(
        self,
        source: str,
        uri: str,
        input_sha: str,
        output_sha: str | None,
        status: str,
        method: str | None,
        message: str | None,
        rows: list[dict],
        run_at: str,
    ) -> None:
        """Replace *uri*'s audit, carrying the change history forward."""
        previous = local_state.get_pre_process_document(source, uri)
        if previous is None:
            previous_input, hash_changed_at = None, run_at
        elif previous["input_sha256"] == input_sha:
            previous_input, hash_changed_at = previous["previous_input_sha256"], previous["hash_changed_at"]
        else:
            previous_input, hash_changed_at = previous["input_sha256"], run_at
        local_state.record_pre_process(
            source,
            {
                "uri": uri,
                "input_sha256": input_sha,
                "output_sha256": output_sha,
                "previous_input_sha256": previous_input,
                "status": str(status),
                "method": method,
                "message": message,
                "hash_changed_at": hash_changed_at,
                "run_at": run_at,
            },
            rows,
        )


def _suffix(message: str | None) -> str:
    return f": {message}" if message else ""


def _step_row(resolved: ResolvedStep, status: str, message: str | None, input_sha: str, output_sha: str, run_at: str) -> dict:
    return {
        "step": resolved.index,
        "method": resolved.step.method,
        "status": str(status),
        "message": message,
        "input_sha256": input_sha,
        "output_sha256": output_sha,
        "run_at": run_at,
    }


def _interpret(value: Any, workdir: Path, name: str) -> tuple[PreProcessResult, Path | None]:
    """Validate a step's answer; return it and, for MODIFIED, the file holding the new content.

    Raises:
        TypeError, ValueError: for an answer the contract does not allow.
        FileNotFoundError: when a MODIFIED ``path`` names no file.
    """
    result = callables.normalize(value, PreProcessResult, PreProcessStatus, PreProcessStatus)
    if result.metadata is not None and not isinstance(result.metadata, dict):
        raise TypeError(f"metadata must be a dict, not {type(result.metadata).__name__}")
    if result.status is PreProcessStatus.MODIFIED:
        return result, _modified_output(result, workdir, name)
    if result.data is not None or result.path is not None:
        raise ValueError(f"{result.status.value} may not return data or path; only modified does")
    return result, None


def _modified_output(result: PreProcessResult, workdir: Path, name: str) -> Path:
    """The file holding a MODIFIED step's new content.

    Raises:
        ValueError: unless exactly one of ``data`` / ``path`` is given.
        FileNotFoundError: when ``path`` names no file.
    """
    if (result.data is None) == (result.path is None):
        raise ValueError("modified requires exactly one of data or path; return PreProcessResult(..., data=...)")
    if result.data is not None:
        if not isinstance(result.data, bytes):
            raise TypeError(f"data must be bytes, not {type(result.data).__name__}")
        output = workdir / name
        output.write_bytes(result.data)
        return output
    output = Path(result.path)
    if not output.is_absolute():
        output = workdir / output
    if not output.is_file():
        raise FileNotFoundError(f"modified path {output} is not a file")
    return output


_active: contextvars.ContextVar[PreProcessRun | None] = contextvars.ContextVar("pre_process_run", default=None)


def current() -> PreProcessRun | None:
    """The pre-process run in effect for this task, if any."""
    return _active.get()


@contextmanager
def activate(run: PreProcessRun) -> Iterator[PreProcessRun]:
    """Make *run* the one :func:`current` returns, for the duration of the block.

    A ContextVar rather than a setting: tasks an agent spawns (webdav's
    concurrent fetches) inherit it, and it is cleared however the block exits.
    """
    token = _active.set(run)
    try:
        yield run
    finally:
        _active.reset(token)
