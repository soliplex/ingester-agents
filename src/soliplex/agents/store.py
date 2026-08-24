"""Where a source's documents live, behind one interface.

A :class:`DownloadTarget` says *where* — the base directory (or, later, bucket
and prefix) plus the per-source folder name. A :class:`DocumentStore` reads and
writes bytes there. Callers pass source-relative keys, exactly as
:func:`~soliplex.agents.local_store.uri_to_relpath` produces them; every layer
of prefixing is the target's business.

Only a local filesystem backend exists here. The point of the seam is that
adding another one is a second :class:`DocumentStore` implementation and a
branch in :func:`get_document_store`, with no call site changing.
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from typing import runtime_checkable
from urllib.parse import unquote

import aiofiles
import aiofiles.os as aos

from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadTarget:
    """Resolved location for one source's documents.

    ``dir`` is the installation's download base; ``source`` is the raw source
    identifier, sanitized into a single folder name by the store. Frozen so it
    can be passed around and compared without anyone mutating it mid-run.
    """

    dir: str
    source: str

    @property
    def is_local(self) -> bool:
        """Whether this target is backed by a filesystem.

        Currently always true. It exists so URI handling can branch on the
        backend rather than on a scheme string parsed at each call site --
        notably in :meth:`key_for_uri`, where a filesystem URI is
        percent-encoded and an object-store URI is not.
        """
        return True

    @property
    def root(self) -> Path:
        """The directory holding this source's documents."""
        from soliplex.agents.local_store import sanitize_source

        return Path(self.dir) / sanitize_source(self.source)

    @property
    def base_uri(self) -> str:
        """The URI prefix every document under this target shares.

        Resolved absolute, because that is the form ``Path.as_uri()`` produces
        and therefore the form stored downstream; a relative base would never
        match its own documents.
        """
        return self.root.resolve().as_uri()

    def uri(self, key: str) -> str:
        """Absolute URI for the document at *key*."""
        return (self.root.resolve() / key).as_uri()

    def key_for_uri(self, uri: str) -> str | None:
        """Source-relative key for *uri*, or None when *uri* is not ours.

        Pure and synchronous: this asks whether the URI falls inside this
        target's address space, not whether an object is present. A URI whose
        document has been deleted still resolves, which is what the
        orphan-cleanup path relies on.

        A trailing ``#attachment=...`` fragment is dropped, so a child document
        resolves to its parent's key.
        """
        uri = uri.split("#", 1)[0]
        base = self.base_uri
        if not uri.startswith(base):
            return None
        rest = uri[len(base) :].lstrip("/")
        # A filesystem URI arrives percent-encoded (``Path.as_uri()``); an
        # object-store URI carries the raw key. Unquoting the latter would
        # corrupt any key containing a percent escape.
        return unquote(rest) if self.is_local else rest


@runtime_checkable
class DocumentStore(Protocol):
    """Byte storage for one source, addressed by source-relative key."""

    target: DownloadTarget

    async def write(self, key: str, data: bytes) -> None:
        """Write *data* at *key*, creating any intermediate structure."""
        ...

    async def read(self, key: str) -> bytes:
        """Return the bytes at *key*.

        Raises:
            FileNotFoundError: if nothing is stored at *key*.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Whether anything is stored at *key*."""
        ...

    async def delete(self, key: str) -> bool:
        """Remove *key*; return False if it was not there."""
        ...

    async def list(self) -> list[str]:
        """Every key under this source, as source-relative POSIX strings."""
        ...

    def uri(self, key: str) -> str:
        """Absolute URI for *key*, for logs and sidecar payloads."""
        ...


class LocalDocumentStore:
    """A :class:`DocumentStore` over the local filesystem."""

    def __init__(self, target: DownloadTarget) -> None:
        self.target = target

    def _path(self, key: str) -> Path:
        return self.target.root / key

    async def write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        await aos.makedirs(path.parent, exist_ok=True)
        async with aiofiles.open(path, "wb") as handle:
            await handle.write(data)

    async def read(self, key: str) -> bytes:
        async with aiofiles.open(self._path(key), "rb") as handle:
            return await handle.read()

    async def exists(self, key: str) -> bool:
        return await aos.path.isfile(self._path(key))

    async def delete(self, key: str) -> bool:
        try:
            await aos.remove(self._path(key))
        except FileNotFoundError:
            return False
        return True

    async def list(self) -> list[str]:
        root = self.target.root
        if not await aos.path.isdir(root):
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def uri(self, key: str) -> str:
        return self.target.uri(key)

    async def destroy(self) -> None:
        """Remove the whole source folder. Only used to reset a source."""
        root = self.target.root
        if await aos.path.isdir(root):
            shutil.rmtree(root)


def get_document_store(source: str, download_dir: str | None = None) -> DocumentStore:
    """Build the store holding *source*'s documents.

    Args:
        source: Raw source identifier; sanitized into the folder name.
        download_dir: Override for ``settings.download_dir`` (mainly tests).

    Returns:
        A :class:`DocumentStore` for that source.
    """
    target = DownloadTarget(
        dir=download_dir if download_dir is not None else settings.download_dir,
        source=source,
    )
    return LocalDocumentStore(target)
