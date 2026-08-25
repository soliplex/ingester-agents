"""Where a source's documents live, behind one interface.

A :class:`DownloadTarget` says *where* — the base directory or bucket prefix,
plus the per-source folder name. A :class:`DocumentStore` reads and writes bytes
there. Callers pass source-relative keys, exactly as
:func:`~soliplex.agents.local_store.uri_to_relpath` produces them; every layer
of prefixing is the target's business.

Two backends: the local filesystem, and S3-compatible object storage via
``obstore`` (the ``s3`` extra). Which one a source gets is decided in
:func:`get_document_store` by whether a bucket is configured; no call site
knows the difference.
"""

import hashlib
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
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

    ``dir`` is the installation's download base and ``source`` the raw source
    identifier, sanitized into a single folder (or key-prefix) segment. Setting
    ``bucket`` makes the target object storage; leaving it unset keeps it on the
    local filesystem. Frozen so it can be passed around and compared without
    anyone mutating it mid-run.
    """

    dir: str
    source: str
    bucket: str | None = None
    storage_options: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        """Whether this target is backed by a filesystem."""
        return self.bucket is None

    @property
    def folder(self) -> str:
        """The single path segment naming this source."""
        from soliplex.agents.local_store import sanitize_source

        return sanitize_source(self.source)

    @property
    def root(self) -> Path:
        """The directory holding this source's documents (local targets only)."""
        return Path(self.dir) / self.folder

    @property
    def prefix(self) -> str:
        """The key prefix holding this source's objects (object targets only).

        Backslashes are normalized to ``/``: a Windows-style ``download_dir``
        is legal in an S3 key but would make the same configuration produce
        different keys per platform.
        """
        base = self.dir.replace("\\", "/").strip("/")
        return "/".join(part for part in (base, self.folder) if part)

    @property
    def base_uri(self) -> str:
        """The URI prefix every document under this target shares.

        A local base is resolved absolute, because that is the form
        ``Path.as_uri()`` produces and therefore what a consumer stores; an
        unresolved base would never match its own documents.
        """
        if self.is_local:
            return self.root.resolve().as_uri()
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"

    def digest(self) -> str:
        """Short stable digest of this target's *configuration*.

        Used to qualify per-target filenames (see
        :func:`~soliplex.agents.local_state.get_state_path`).

        Deliberately derived from the configured values rather than from
        :attr:`base_uri`: a local base URI is resolved against the process's
        working directory, so a relative ``DOWNLOAD_DIR`` would give the same
        configuration a different digest -- and therefore a different state
        file, and therefore a full re-fetch -- when the process runs from
        somewhere else.
        """
        material = repr((self.bucket or "", self.dir.replace(chr(92), "/").strip("/"), self.folder))
        return hashlib.sha256(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    def uri(self, key: str) -> str:
        """Absolute URI for the document at *key*."""
        if self.is_local:
            return (self.root.resolve() / key).as_uri()
        return f"{self.base_uri}/{key}"

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


class S3DocumentStore:
    """A :class:`DocumentStore` over S3-compatible object storage.

    Uses ``obstore``, which is also what the downstream indexer's ``s3`` source
    uses, so both sides consume the same ``storage_options`` vocabulary and a
    credential set configured once works for both.
    """

    def __init__(self, target: DownloadTarget) -> None:
        self.target = target
        self._store = _make_s3_store(target.bucket, dict(target.storage_options))

    def _key(self, key: str) -> str:
        prefix = self.target.prefix
        return f"{prefix}/{key}" if prefix else key

    async def write(self, key: str, data: bytes) -> None:
        import obstore

        await obstore.put_async(self._store, self._key(key), data)

    async def read(self, key: str) -> bytes:
        import obstore

        result = await obstore.get_async(self._store, self._key(key))
        return bytes(await result.bytes_async())

    async def exists(self, key: str) -> bool:
        import obstore

        try:
            await obstore.head_async(self._store, self._key(key))
        except FileNotFoundError:
            return False
        return True

    async def delete(self, key: str) -> bool:
        import obstore

        # obstore's delete is a silent no-op on a missing key, so the bool
        # contract costs a HEAD. Only the stale-cleanup path calls this.
        if not await self.exists(key):
            return False
        await obstore.delete_async(self._store, self._key(key))
        return True

    async def list(self) -> list[str]:
        import obstore

        prefix = self.target.prefix
        keys: list[str] = []
        async for batch in obstore.list(self._store, prefix=prefix or None):
            for obj in batch:
                path = obj["path"]
                keys.append(path[len(prefix) :].lstrip("/") if prefix else path)
        return sorted(keys)

    def uri(self, key: str) -> str:
        return self.target.uri(key)

    async def destroy(self) -> None:
        """Remove every object under this source's prefix."""
        import obstore

        for key in await self.list():
            await obstore.delete_async(self._store, self._key(key))


def _make_s3_store(bucket: str, storage_options: dict[str, str]):
    """Build an obstore ``S3Store`` from LanceDB-style storage options.

    Delegates to haiku.rag's helper so the endpoint / path-style handling that
    non-AWS deployments (MinIO, SeaweedFS) need is decided in one place, and so
    the writer cannot drift from the source that reads what it wrote.
    """
    from haiku.rag.s3 import make_s3_store

    return make_s3_store(bucket, storage_options)


# Stores are cached per resolved target because `write_document` resolves one
# per document: building an S3Store means building an HTTP client and
# connection pool, and doing that inside the write loop is pure waste. The key
# is the whole target, so a settings change yields a different store rather
# than a stale one.
_STORES: dict[tuple, "DocumentStore"] = {}


def storage_options() -> dict[str, str]:
    """Assemble obstore/LanceDB-style storage options from the settings.

    Empty values are omitted so an unset credential falls through to the AWS
    default chain (environment, instance role, profile) rather than being
    passed as an empty string.
    """
    raw = {
        "aws_access_key_id": settings.s3_access_key_id,
        "aws_secret_access_key": (
            settings.s3_secret_access_key.get_secret_value() if settings.s3_secret_access_key else None
        ),
        "region": settings.s3_region,
        "endpoint": settings.s3_endpoint_url,
    }
    options = {key: value for key, value in raw.items() if value}
    if settings.s3_allow_http:
        options["allow_http"] = "true"
    return options


def get_document_store(source: str, download_dir: str | None = None) -> DocumentStore:
    """Build the store holding *source*'s documents.

    The backend is chosen by whether ``download_s3_bucket`` is configured.
    ``download_dir`` keeps one meaning either way: the base a source's folder
    sits under, a local directory or a key prefix.

    Args:
        source: Raw source identifier; sanitized into the folder name.
        download_dir: Override for ``settings.download_dir`` (mainly tests).

    Returns:
        A :class:`DocumentStore` for that source.
    """
    target = DownloadTarget(
        dir=download_dir if download_dir is not None else settings.download_dir,
        source=source,
        bucket=settings.download_s3_bucket,
        storage_options=storage_options() if settings.download_s3_bucket else {},
    )
    cache_key = (target.dir, target.source, target.bucket, tuple(sorted(target.storage_options.items())))
    store = _STORES.get(cache_key)
    if store is None:
        store = LocalDocumentStore(target) if target.is_local else S3DocumentStore(target)
        _STORES[cache_key] = store
    return store


def reset_store_cache() -> None:
    """Drop every cached store. For tests, and for a settings change at runtime."""
    _STORES.clear()
