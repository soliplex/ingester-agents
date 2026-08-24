"""The ``.meta.json`` sidecar: what the indexing step cannot see for itself.

A downloaded document arrives at the indexer as bytes under a local path or
object key. Its MIME type, the upstream URI it came from, and any metadata the
manifest attached are all lost in that handoff, so they are written beside it.

:meth:`MetaSidecar.build` produces the payload; :meth:`MetaSidecar.parse` reads
it back flattened, promoting the nested ``metadata`` sub-dict to the top level
so consumers get one flat mapping. The pair is therefore not strictly inverse,
which is why the round-trip test asserts against the flattened shape.
"""

import hashlib
import json
import logging
from typing import Any
from typing import ClassVar

from soliplex.agents.sidecar import DocumentWrite
from soliplex.agents.sidecar import SidecarKind
from soliplex.agents.sidecar import register

logger = logging.getLogger(__name__)

# Payload key holding manifest-supplied metadata. Its entries are promoted to
# the top level on read and the key itself is dropped.
NESTED_KEY = "metadata"


def _coerce_value(value: Any) -> Any:
    """Make one metadata value store-safe.

    A consumer typically persists metadata as a single JSON blob, so
    JSON-native scalars are kept as-is (``size`` stays an int) while nested
    lists and dicts are JSON-encoded to keep the mapping flat and
    filter-friendly. ``None`` values are dropped by the caller.
    """
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, default=str, sort_keys=True)


def _flatten(payload: dict) -> dict:
    """Promote the nested ``metadata`` sub-dict, preserving top-level keys.

    Nested (operator-supplied) keys win over a same-named top-level key.
    ``None`` values are dropped; nested non-scalars are JSON-encoded.
    """
    nested = payload.get(NESTED_KEY)
    merged: dict[str, Any] = {key: value for key, value in payload.items() if key != NESTED_KEY}
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key in merged:
                logger.debug("sidecar metadata key %r overridden by nested value", key)
            merged[key] = value
    elif nested is not None:
        logger.warning("sidecar %r key is not a mapping (%s); ignoring", NESTED_KEY, type(nested).__name__)
    return {key: _coerce_value(value) for key, value in merged.items() if value is not None}


@register
class MetaSidecar(SidecarKind):
    """Document provenance and manifest metadata, as JSON."""

    kind: ClassVar[str] = "meta"
    suffix: ClassVar[str] = ".meta.json"

    def build(self, doc: DocumentWrite) -> bytes:
        payload = {
            "mime_type": doc.mime_type,
            "source": doc.source,
            "source_uri": doc.uri,
            "ingestion_type": doc.ingestion_type,
            "sha256": hashlib.sha256(doc.content, usedforsecurity=False).hexdigest(),
            "size": len(doc.content),
            NESTED_KEY: doc.metadata or {},
        }
        if doc.source_url is not None:
            payload["source_url"] = doc.source_url
        return json.dumps(payload, indent=2, default=str).encode("utf-8")

    def parse(self, content: bytes) -> dict:
        """Return the payload flattened, or ``{}`` when it is unusable.

        Lenient about *format* -- invalid JSON or a non-object top level yield
        an empty mapping -- because a malformed sidecar should degrade a single
        document's metadata rather than fail the run that reads it. Absence is
        the caller's business, not this method's.
        """
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("sidecar is not valid JSON; ignoring")
            return {}
        if not isinstance(payload, dict):
            logger.warning("sidecar top level is %s, not an object; ignoring", type(payload).__name__)
            return {}
        return _flatten(payload)
