"""Post-copy file processors for ingested documents.

Processors rewrite files on disk after they are copied to the download
directory. Each processor targets one or more MIME types and is registered
via the :func:`register` decorator. Calling :func:`run_processors` runs
every registered processor for a given MIME type in registration order.

Adding a new processor:

    from soliplex.agents.common.processors import FileProcessor, register

    @register("text/mytype")
    class MyProcessor(FileProcessor):
        def process(self, path: Path, mime_type: str) -> None:
            ...
"""

import logging
from abc import ABC
from abc import abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, list[type["FileProcessor"]]] = {}


class ProcessorRejected(Exception):
    """Raised by a processor to signal the file should be discarded.

    The caller is responsible for removing the file and its sidecar from
    the download directory and omitting the URI from local state.
    """


class FileProcessor(ABC):
    """Base class for post-copy file processors."""

    @abstractmethod
    def process(self, path: Path, mime_type: str) -> None:
        """Rewrite *path* in place if corrections are needed."""


def register(*mime_types: str):
    """Class decorator that registers a processor for one or more MIME types."""

    def decorator(cls: type[FileProcessor]) -> type[FileProcessor]:
        for mime_type in mime_types:
            _REGISTRY.setdefault(mime_type, []).append(cls)
        return cls

    return decorator


def run_processors(path: Path, mime_type: str) -> None:
    """Run all processors registered for *mime_type* against *path*.

    Raises:
        ProcessorRejected: if a processor rejects the file. The caller is
            responsible for removing the file from the download directory.
    """
    for cls in _REGISTRY.get(mime_type, []):
        try:
            cls().process(path, mime_type)
        except ProcessorRejected:
            raise
        except Exception:
            logger.exception("Processor %s failed on %s", cls.__name__, path)


# Register built-in processors (side-effect imports).
from . import asciidoc as _asciidoc  # noqa: E402, F401
from . import pdf as _pdf  # noqa: E402, F401
