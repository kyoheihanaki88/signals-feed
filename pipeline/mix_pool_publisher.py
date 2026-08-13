#!/usr/bin/env python3
"""Provider-neutral Mix Pool publisher. (Phase 3D-3B)

NOT CONNECTED TO A REAL PROVIDER. No storage backend is provisioned in this repository:
`KV_REST_API_*` appear only in `api/` source and tests, GitHub Actions holds no KV write
credential, and the only object storage is the PUBLIC audio R2 bucket. This module defines
the publishing contract and every pre-flight check; the transport is an injected seam with
no production implementation yet.

WHAT IT GUARANTEES:
  • only an already-frozen, schema-valid artifact is ever sent
  • `poolIdentity` is re-derived and compared before the transport is touched
  • the artifact's own date must equal the publication date
  • a size ceiling is enforced before any network call
  • exactly ONE atomic write per publication — never a multi-step assembly
  • the result carries safe metadata only: no candidate content, no headline, no URL,
    no credential

It does not touch standard edition generation, and it never writes into the repository —
the existing `mix_pool_cli.py` destination protection is untouched and complementary.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Protocol

try:
    from .mix_pool_schema import (
        GENERATOR_VERSION,
        SCHEMA_VERSION,
        pool_identity,
        serialize,
        validate_artifact,
    )
    from .mix_identity import POOL_SELECTOR_VERSION as SELECTOR_VERSION
except ImportError:  # direct script/module use, matching the rest of the pipeline
    from mix_pool_schema import (  # type: ignore[no-redef]
        GENERATOR_VERSION,
        SCHEMA_VERSION,
        pool_identity,
        serialize,
        validate_artifact,
    )
    from mix_identity import POOL_SELECTOR_VERSION as SELECTOR_VERSION  # type: ignore[no-redef]

#: Shared with `api/_lib/mix-pool-source.ts`. Both sides must build the same key.
KEY_NAMESPACE = "signals:mix-pool"
KEY_VERSION = "v1"

#: Must not exceed the reader's MAX_POOL_BYTES.
MAX_POOL_BYTES = 2 * 1024 * 1024
#: Slightly beyond the reader's MAX_POOL_AGE_MS so expiry is never the limiting factor.
DEFAULT_TTL_SECONDS = 9 * 24 * 60 * 60

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MixPoolPublishError(ValueError):
    """A publication refused before the transport was touched. Carries no payload."""


class PoolObjectStore(Protocol):
    """The transport seam: one atomic whole-object write."""

    def put(self, key: str, body: bytes, *, ttl_seconds: int | None) -> None:
        ...


def mix_pool_key(date: str) -> str:
    """`signals:mix-pool:v1:YYYY-MM-DD`. UTC date only — no identity, no preferences."""
    if not _DATE_RE.fullmatch(str(date)):
        raise MixPoolPublishError("date must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise MixPoolPublishError("date must be a real calendar date") from exc
    return f"{KEY_NAMESPACE}:{KEY_VERSION}:{date}"


def publish_mix_pool(
    artifact: dict[str, Any],
    *,
    store: PoolObjectStore | None,
    date: str | None = None,
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
    max_bytes: int = MAX_POOL_BYTES,
    serializer: Callable[[Any], bytes] = serialize,
) -> dict[str, Any]:
    """
    Publish one frozen artifact atomically and return safe metadata.

    Every check runs BEFORE the transport, so a rejected artifact never produces a partial
    write. Raises `MixPoolPublishError` for a contract failure; a transport failure
    propagates unchanged so the caller can distinguish "we refused" from "the store broke".
    """
    if not isinstance(artifact, dict):
        raise MixPoolPublishError("artifact must be an object")

    publication_date = date if date is not None else artifact.get("date")
    key = mix_pool_key(str(publication_date))

    # The artifact must prove its own date; the key alone is not evidence.
    if artifact.get("date") != publication_date:
        raise MixPoolPublishError("artifact date does not match the publication date")

    if artifact.get("schemaVersion") != SCHEMA_VERSION:
        raise MixPoolPublishError("unsupported schemaVersion")
    if artifact.get("selectorVersion") != SELECTOR_VERSION:
        raise MixPoolPublishError("unsupported selectorVersion")
    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("generatorVersion") != GENERATOR_VERSION:
        raise MixPoolPublishError("unsupported provenance.generatorVersion")

    validation = validate_artifact(artifact)
    if not validation["valid"]:
        # Field paths only — `validate_artifact` never puts payload in an error.
        raise MixPoolPublishError(
            "artifact failed schema validation: " + "; ".join(validation["errors"][:5])
        )

    candidates = artifact.get("candidates") or []
    if pool_identity(candidates) != artifact.get("poolIdentity"):
        raise MixPoolPublishError("poolIdentity does not match candidates")
    if artifact.get("candidateCount") != len(candidates):
        raise MixPoolPublishError("candidateCount does not match candidates")
    if not candidates:
        raise MixPoolPublishError("refusing to publish an empty candidate pool")

    body = serializer(artifact)
    if len(body) > max_bytes:
        raise MixPoolPublishError(f"artifact exceeds {max_bytes} bytes")

    if store is None:
        raise MixPoolPublishError("no candidate pool store is configured")

    # ONE call. A provider without atomic whole-object write is not a valid backend here.
    store.put(key, body, ttl_seconds=ttl_seconds)

    return {
        "status": "published",
        "date": artifact["date"],
        "key": key,
        "schemaVersion": artifact["schemaVersion"],
        "selectorVersion": artifact["selectorVersion"],
        "candidateCount": len(candidates),
        "byteLength": len(body),
        # A prefix only: the full identity is a content fingerprint.
        "poolIdentityPrefix": str(artifact["poolIdentity"])[:12],
        "ttlSeconds": ttl_seconds,
    }
