"""Content fixups applied to documents before they are stored.

Processors transform a document's bytes on the way to the download store.
Each processor targets one or more MIME types and is registered via the
:func:`register` decorator. Calling :func:`run_processors` runs every
registered processor for a given MIME type in registration order, threading
the bytes through each.

Processors run *before* the write, so a rejected document is never stored and
there is nothing to clean up.

Adding a new processor:

    from soliplex.agents.common.processors import FileProcessor, register

    @register("text/mytype")
    class MyProcessor(FileProcessor):
        def process(self, data: bytes, mime_type: str) -> bytes:
            ...
"""

import logging
from abc import ABC
from abc import abstractmethod

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, list[type["FileProcessor"]]] = {}


class ProcessorRejected(Exception):
    """Raised by a processor to signal the document should be discarded.

    Raised before anything is written, so the caller only has to omit the URI
    from local state and record the rejection.
    """


class FileProcessor(ABC):
    """Base class for content fixups."""

    @abstractmethod
    def process(self, data: bytes, mime_type: str) -> bytes:
        """Return corrected bytes, or *data* unchanged."""


def register(*mime_types: str):
    """Class decorator that registers a processor for one or more MIME types."""

    def decorator(cls: type[FileProcessor]) -> type[FileProcessor]:
        for mime_type in mime_types:
            _REGISTRY.setdefault(mime_type, []).append(cls)
        return cls

    return decorator


def run_processors(data: bytes, mime_type: str) -> bytes:
    """Run every processor registered for *mime_type* over *data*.

    Each processor's output feeds the next. A processor that raises anything
    other than :class:`ProcessorRejected` is logged and skipped, leaving the
    bytes it was given untouched.

    Args:
        data: The document's bytes.
        mime_type: Resolved MIME type selecting which processors run.

    Returns:
        The bytes to store.

    Raises:
        ProcessorRejected: if a processor rejects the document. Nothing has
            been written at this point.
    """
    for cls in _REGISTRY.get(mime_type, []):
        try:
            data = cls().process(data, mime_type)
        except ProcessorRejected:
            raise
        except Exception:
            logger.exception("Processor %s failed on %s content", cls.__name__, mime_type)
    return data


# Register built-in processors (side-effect imports).
from . import asciidoc as _asciidoc  # noqa: E402, F401
from . import pdf as _pdf  # noqa: E402, F401
