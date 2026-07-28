#!/usr/bin/env python3
"""Provider-neutral Editorial Mix Pool publisher. (Phase 3D-3D)

The Phase 3D-3B publisher (`mix_pool_publisher.py`) covers the RAW selector pool. Its
schema checks are the raw schema's, so it cannot validate an enriched artifact. Rather than
loosening that module — the contract it enforces is exactly right for what it publishes —
this file adds the sibling policy for the ENRICHED artifact, reusing the same
`PoolObjectStore` protocol, the same one-atomic-write rule and the same safe-result shape.

WHAT IT GUARANTEES, all of it BEFORE the transport is touched:
  • the artifact is a frozen, schema-valid Editorial Mix Pool
  • BOTH identities are re-derived from the candidates and compared
  • the surviving candidate count is at least MINIMUM_PUBLISHABLE_POOL_SIZE (15) and at
    most TARGET_POOL_SIZE (20)
  • the artifact's own date equals the publication date — the key alone is not evidence
  • the serialized body is under the reader's byte ceiling
  • exactly ONE atomic write, so a rerun for the same date supersedes rather than merges

The returned result carries safe metadata only: no headline, no summary, no URL, no
credential, and identity PREFIXES rather than whole content fingerprints.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

try:
    from .editorial_mix_pool_schema import (
        ARTIFACT_TYPE,
        EDITORIAL_VERSION,
        GENERATOR_VERSION,
        MINIMUM_PUBLISHABLE_POOL_SIZE,
        SCHEMA_VERSION,
        SELECTOR_VERSION,
        TARGET_POOL_SIZE,
        editorial_pool_identity,
        selector_pool_identity,
        validate_editorial_mix_pool,
    )
    from .mix_pool_publisher import MAX_POOL_BYTES, PoolObjectStore
    from .mix_pool_schema import serialize
except ImportError:  # direct script/module use, matching the rest of the pipeline
    from editorial_mix_pool_schema import (  # type: ignore[no-redef]
        ARTIFACT_TYPE,
        EDITORIAL_VERSION,
        GENERATOR_VERSION,
        MINIMUM_PUBLISHABLE_POOL_SIZE,
        SCHEMA_VERSION,
        SELECTOR_VERSION,
        TARGET_POOL_SIZE,
        editorial_pool_identity,
        selector_pool_identity,
        validate_editorial_mix_pool,
    )
    from mix_pool_publisher import MAX_POOL_BYTES, PoolObjectStore  # type: ignore[no-redef]
    from mix_pool_schema import serialize  # type: ignore[no-redef]

#: Its OWN namespace. The enriched pool must never be mistaken for the raw selector pool,
#: even if both ever lived in one database. Shared with the TypeScript side.
KEY_NAMESPACE = "signals:editorial-mix-pool"
KEY_VERSION = "v1"

#: Slightly beyond the reader's freshness ceiling so expiry is never the limiting factor.
DEFAULT_TTL_SECONDS = 9 * 24 * 60 * 60

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class EditorialPublishError(ValueError):
    """A publication refused before the transport was touched. Carries no payload."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


REASON_INVALID_ARTIFACT = "editorial_publish_invalid_artifact"
REASON_VERSION_MISMATCH = "editorial_publish_version_mismatch"
REASON_DATE_MISMATCH = "editorial_publish_date_mismatch"
REASON_IDENTITY_MISMATCH = "editorial_publish_identity_mismatch"
REASON_INSUFFICIENT_CANDIDATES = "editorial_publish_insufficient_candidates"
REASON_TOO_MANY_CANDIDATES = "editorial_publish_too_many_candidates"
REASON_TOO_LARGE = "editorial_publish_too_large"
REASON_NO_STORE = "editorial_publish_no_store"
REASON_ARTIFACT_ENCODING = "artifact_utf8_encoding_failed"


def editorial_mix_pool_key(date: str) -> str:
    """`signals:editorial-mix-pool:v1:YYYY-MM-DD`. UTC date only — no identity, no preferences."""
    if not _DATE_RE.fullmatch(str(date)):
        raise EditorialPublishError(REASON_DATE_MISMATCH, "date must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(str(date))
    except ValueError as exc:
        raise EditorialPublishError(
            REASON_DATE_MISMATCH, "date must be a real calendar date"
        ) from exc
    return f"{KEY_NAMESPACE}:{KEY_VERSION}:{date}"


def publish_editorial_mix_pool(
    artifact: dict[str, Any],
    *,
    store: PoolObjectStore | None,
    date: str | None = None,
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
    max_bytes: int = MAX_POOL_BYTES,
    serializer: Callable[[Any], bytes] = serialize,
    minimum_candidates: int = MINIMUM_PUBLISHABLE_POOL_SIZE,
) -> dict[str, Any]:
    """
    Publish one frozen Editorial Mix Pool atomically and return safe metadata.

    Every check runs BEFORE the transport, so a rejected artifact never produces a partial
    write. Raises `EditorialPublishError` for a contract failure; a transport failure
    propagates unchanged so the caller can distinguish "we refused" from "the store broke".
    """
    if not isinstance(artifact, dict):
        raise EditorialPublishError(REASON_INVALID_ARTIFACT, "artifact must be an object")

    publication_date = date if date is not None else artifact.get("date")
    key = editorial_mix_pool_key(str(publication_date))

    # The artifact must prove its own date; the key alone is not evidence.
    if artifact.get("date") != publication_date:
        raise EditorialPublishError(REASON_DATE_MISMATCH)

    if artifact.get("artifactType") != ARTIFACT_TYPE:
        raise EditorialPublishError(REASON_VERSION_MISMATCH, "artifactType")
    if artifact.get("schemaVersion") != SCHEMA_VERSION:
        raise EditorialPublishError(REASON_VERSION_MISMATCH, "schemaVersion")
    if artifact.get("selectorVersion") != SELECTOR_VERSION:
        raise EditorialPublishError(REASON_VERSION_MISMATCH, "selectorVersion")
    if artifact.get("editorialVersion") != EDITORIAL_VERSION:
        raise EditorialPublishError(REASON_VERSION_MISMATCH, "editorialVersion")
    provenance = artifact.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("generatorVersion") != GENERATOR_VERSION
    ):
        raise EditorialPublishError(REASON_VERSION_MISMATCH, "provenance.generatorVersion")

    # A LONE SURROGATE cannot be encoded as UTF-8, and the identity check inside the
    # validator is the first thing to touch it — earlier than serialization. Catch it here
    # so the artifact is REJECTED with a stable category instead of the raw exception
    # escaping as a bare class name. Nothing is replaced and nothing is transliterated.
    try:
        validation = validate_editorial_mix_pool(artifact)
    except UnicodeEncodeError:
        raise EditorialPublishError(REASON_ARTIFACT_ENCODING) from None
    if not validation["valid"]:
        # Field paths only — the validator never puts payload in an error.
        raise EditorialPublishError(
            REASON_INVALID_ARTIFACT, "; ".join(validation["errors"][:5])
        )

    candidates = artifact.get("candidates") or []
    if not isinstance(candidates, list):
        raise EditorialPublishError(REASON_INVALID_ARTIFACT, "candidates")
    if artifact.get("candidateCount") != len(candidates):
        raise EditorialPublishError(REASON_INVALID_ARTIFACT, "candidateCount")

    # The product contract, enforced at the last gate before publication.
    if len(candidates) < minimum_candidates:
        raise EditorialPublishError(
            REASON_INSUFFICIENT_CANDIDATES, f"{len(candidates)} < {minimum_candidates}"
        )
    if len(candidates) > TARGET_POOL_SIZE:
        raise EditorialPublishError(
            REASON_TOO_MANY_CANDIDATES, f"{len(candidates)} > {TARGET_POOL_SIZE}"
        )

    # Re-derive BOTH identities rather than trusting the stored values.
    selectors = [(row.get("selector") or {}) for row in candidates]
    if selector_pool_identity(artifact) != artifact.get("selectorPoolIdentity"):
        raise EditorialPublishError(REASON_IDENTITY_MISMATCH, "selectorPoolIdentity")
    if editorial_pool_identity(candidates) != artifact.get("editorialPoolIdentity"):
        raise EditorialPublishError(REASON_IDENTITY_MISMATCH, "editorialPoolIdentity")

    # THE ONE str -> bytes BOUNDARY. `serialize` emits UTF-8 bytes with ensure_ascii=False,
    # so the stored value keeps its Unicode exactly. A lone surrogate is the only input a
    # valid Python string can carry that UTF-8 cannot represent; it is REJECTED here rather
    # than replaced, so no artifact is ever silently mangled.
    try:
        body = serializer(artifact)
    except UnicodeEncodeError:
        raise EditorialPublishError(REASON_ARTIFACT_ENCODING) from None
    if len(body) > max_bytes:
        raise EditorialPublishError(REASON_TOO_LARGE, f"{len(body)} > {max_bytes}")

    if store is None:
        raise EditorialPublishError(REASON_NO_STORE)

    # ONE call. A provider without atomic whole-object write is not a valid backend here.
    store.put(key, body, ttl_seconds=ttl_seconds)

    return {
        "status": "published",
        "date": artifact["date"],
        "key": key,
        "artifactType": artifact["artifactType"],
        "schemaVersion": artifact["schemaVersion"],
        "selectorVersion": artifact["selectorVersion"],
        "editorialVersion": artifact["editorialVersion"],
        "candidateCount": len(candidates),
        "selectorCount": len(selectors),
        "byteLength": len(body),
        # Prefixes only: a whole identity is a content fingerprint.
        "selectorPoolIdentityPrefix": str(artifact["selectorPoolIdentity"])[:12],
        "editorialPoolIdentityPrefix": str(artifact["editorialPoolIdentity"])[:12],
        "ttlSeconds": ttl_seconds,
    }
