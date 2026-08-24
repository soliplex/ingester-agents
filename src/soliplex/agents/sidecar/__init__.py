"""Sidecars: the extra objects stored alongside each downloaded document.

One kind exists today -- ``.meta.json``, carrying the document's MIME type,
its upstream URI, and any manifest-supplied metadata -- because the indexing
step reads only the document bytes and would otherwise never see them.

Everything about a kind lives in one :class:`SidecarKind` subclass: its
suffix, how its content is built at write time, and how that content is read
back. :class:`Sidecars` owns addressing, so callers never construct a key or a
suffix. Adding a kind is adding a class and one import line at the foot of this
module.

That matters beyond tidiness. The suffix used to be spelled out in four
places -- the write, the delete, the reconcile sweep's ``expected`` set, and a
reader in another repository -- and the sweep deletes anything not in
``expected``. A second kind added without touching the sweep would have been
destroyed on the next run. Deriving ``expected`` from the registry
(:meth:`Sidecars.expected_keys`) removes that by construction.
"""

import logging
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import ClassVar

from soliplex.agents.store import DocumentStore

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type["SidecarKind"]] = {}


@dataclass(frozen=True)
class DocumentWrite:
    """The facts a sidecar can be built from, as one document is stored.

    Deliberately the arguments ``write_document`` already takes, plus the
    content, because those are what all four agents demonstrably supply.
    Anything richer -- an ETag, a PROPFIND property set, a commit sha -- is
    held by only one agent, so a kind needing it should be written by that
    agent through :meth:`Sidecars.write` rather than by widening this.
    """

    source: str
    uri: str
    content: bytes
    mime_type: str | None = None
    metadata: dict = field(default_factory=dict)
    ingestion_type: str | None = None
    source_url: str | None = None


class SidecarKind(ABC):
    """One kind of sidecar: its suffix, how to build it, how to read it.

    Named *Kind* rather than *Sidecar* so it is not confused with the
    :class:`Sidecars` facade.
    """

    kind: ClassVar[str]
    suffix: ClassVar[str]

    @abstractmethod
    def build(self, doc: DocumentWrite) -> bytes | None:
        """This kind's content for *doc*, or None to write nothing for it."""

    @abstractmethod
    def parse(self, content: bytes) -> dict:
        """Interpret content this kind previously produced.

        *content* is this sidecar's own bytes, not the document's -- ``parse``
        is the inverse of ``build``, and a reader has nothing but the object.
        """


def register(cls: type[SidecarKind]) -> type[SidecarKind]:
    """Class decorator registering a sidecar kind."""
    _REGISTRY[cls.kind] = cls
    return cls


def kinds() -> dict[str, type[SidecarKind]]:
    """Registered kinds, keyed by name."""
    return dict(_REGISTRY)


def get_kind(kind: str) -> SidecarKind:
    """Instantiate a registered kind by name.

    Raises:
        KeyError: if *kind* is not registered.
    """
    return _REGISTRY[kind]()


class Sidecars:
    """The sidecars attached to documents in one or more download targets.

    Holds stores rather than being one: backends stay the store's business.
    Multiple stores are accepted so a set of documents spanning two targets --
    which happens part-way through a storage migration -- can be read without
    the caller knowing which target owns a given URI.
    """

    def __init__(self, stores: Sequence[DocumentStore] | DocumentStore) -> None:
        self._stores = [stores] if isinstance(stores, DocumentStore) else list(stores)

    @property
    def store(self) -> DocumentStore:
        """The store writes go to (the first one given)."""
        return self._stores[0]

    def key_of(self, document_key: str, kind: str) -> str:
        """Storage key for *kind*'s sidecar of the document at *document_key*."""
        return document_key + _REGISTRY[kind].suffix

    def expected_keys(self, document_key: str) -> set[str]:
        """Every sidecar key a document could have, for the reconcile sweep.

        Derived from the registry, so a registered kind is preserved without
        the sweep needing to know it exists.
        """
        return {document_key + cls.suffix for cls in _REGISTRY.values()}

    async def write_all(self, document_key: str, doc: DocumentWrite) -> None:
        """Build and store every registered kind that wants one for *doc*."""
        for name, cls in _REGISTRY.items():
            content = cls().build(doc)
            if content is not None:
                await self.store.write(self.key_of(document_key, name), content)

    async def write(self, document_key: str, kind: str, content: bytes) -> None:
        """Store *content* as *kind*'s sidecar, for a caller that built it itself."""
        await self.store.write(self.key_of(document_key, kind), content)

    async def delete_all(self, document_key: str) -> bool:
        """Remove every kind's sidecar for a document; True if any existed."""
        removed = False
        for name in _REGISTRY:
            if await self.store.delete(self.key_of(document_key, name)):
                removed = True
        return removed

    async def read_for_uri(self, uri: str, kind: str = "meta") -> bytes | None:
        """Read *kind*'s sidecar for the document stored at *uri*.

        Returns None when no store owns *uri*, or when that store holds no
        sidecar of this kind. Absence is not an error: a document written
        before the kind existed simply has none.
        """
        for store in self._stores:
            key = store.target.key_for_uri(uri)
            if key is None:
                continue
            try:
                return await store.read(self.key_of(key, kind))
            except FileNotFoundError:
                return None
        logger.info("no download target owns %s; no sidecar", uri)
        return None


# Register built-in kinds (side-effect import).
from soliplex.agents.sidecar import meta as _meta  # noqa: E402, F401

META_SUFFIX = _meta.MetaSidecar.suffix
