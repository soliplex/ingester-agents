"""The facts one source's load needs, assembled once.

`SOURCE` and `DOWNLOAD_DIR` used to be built into an environment dict in three
places -- the load subprocess, the maintenance subprocess, and a temporary
mutation of the process's own `os.environ` for in-process callbacks -- with the
second's docstring saying it mirrored the first. Adding anything to that
channel meant editing all three.

:class:`LoadContext` is that set of facts as one value object. The subprocess
builders serialize it into `env`, which is the only boundary that genuinely
needs environment variables; in-process callbacks receive it directly.
"""

import logging
import os
from dataclasses import dataclass
from dataclasses import field

from soliplex.agents.local_store import sanitize_source
from soliplex.agents.sidecar import Sidecars
from soliplex.agents.store import DocumentStore
from soliplex.agents.store import DownloadTarget
from soliplex.agents.store import get_document_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadContext:
    """Everything a load and its post-process callbacks need about one source.

    ``source`` is the raw manifest source; ``sanitized`` is the download-folder
    name derived from it, which is what a haiku config's ``${SOURCE}`` resolves
    to. ``store`` and ``sidecars`` are the resolved storage handles, so a
    callback never has to rediscover its own environment.
    """

    source: str
    sanitized: str
    store: DocumentStore
    sidecars: Sidecars
    storage_options: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_source(cls, source: str, download_dir: str | None = None) -> "LoadContext":
        """Resolve the context for *source* from the installation settings."""
        store = get_document_store(source, download_dir)
        return cls(
            source=source,
            sanitized=sanitize_source(source),
            store=store,
            sidecars=Sidecars(store),
        )

    @property
    def target(self) -> DownloadTarget:
        """Where this source's documents live."""
        return self.store.target

    @property
    def download_uri(self) -> str:
        """The base URI a consumer should read this source's documents from.

        Set in every mode, so a haiku config can use one form -- currently the
        source folder's ``file://`` URI.
        """
        return self.target.base_uri

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """*base* (default: the current environment) plus this context's vars.

        The one place environment variables are still warranted: a subprocess
        boundary genuinely needs them.
        """
        env = dict(os.environ if base is None else base)
        env["SOURCE"] = self.sanitized
        env["DOWNLOAD_DIR"] = self.target.dir
        env["DOWNLOAD_URI"] = self.download_uri
        for key, value in self.storage_options.items():
            env[f"DOWNLOAD_S3_{key.upper()}"] = value
        return env
