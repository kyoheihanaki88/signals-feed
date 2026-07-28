#!/usr/bin/env python3
"""Editorial Mix Pool artifact contract. (Phase 3D-3C.1)

A SEPARATE artifact from the raw Mix Pool. Raw schema version 1 stays untouched; this adds
the story-level editorial fields the iOS client needs, so a selected five can become a
`SignalsFeed` with no request-time generation.

STORY-LEVEL vs EDITION-LEVEL. Story-level fields belong to a candidate and are stable
wherever it appears (headline, summary, keyTakeaways, whyItMatters, source, originalURL,
readTime, imageURL, placeTime, audioURL, category). Edition-level fields depend on WHICH
five were chosen and in what order (number, importance, lead, date, focus, version) and are
therefore REJECTED inside a stored candidate — the same story is number 1 in one mix and
number 4 in another.

`focus` is constant: `build.py` sets `FOCUS = "MIXED"` and `VERSION = 1` as module
constants, neither derived from the selection. Listen is not a decoding blocker: `audioURL`
is `""` in the live feed and `listen`/`localized` are optional in Swift.

TWO IDENTITIES. `selectorPoolIdentity` hashes only the extracted selector candidates,
reusing `mix_pool_schema.pool_identity`, so it must reproduce the original raw pool value —
proof that enrichment did not disturb selection inputs. `editorialPoolIdentity` hashes the
whole enriched set, so editorial drift is distinguishable from selector drift.

All raw candidate rules — the numeric contract, taxonomy, evidence — are DELEGATED to
`mix_pool_schema`. There is no second copy.

This module defines the contract only. It does not fetch articles, run the writer, assign
images or publish anything; that is Phase 3D-3C.2. Future targets: pool size 20, minimum
publishable count 15, images assigned offline with no duplicate inside the pool and the
existing 90-edition cooldown retained.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

try:
    from .mix_pool_schema import canonical_bytes, pool_identity, validate_artifact
except ImportError:  # direct script/module use, matching the rest of the pipeline
    from mix_pool_schema import (  # type: ignore[no-redef]
        canonical_bytes,
        pool_identity,
        validate_artifact,
    )

ARTIFACT_TYPE = "editorial-mix-pool"
SCHEMA_VERSION = 1
SELECTOR_VERSION = 1
EDITORIAL_VERSION = 1
GENERATOR_VERSION = 1

#: Future pipeline targets, recorded beside the contract so the two cannot drift.
TARGET_POOL_SIZE = 20
MINIMUM_PUBLISHABLE_POOL_SIZE = 15

TOP_KEYS = {
    "artifactType", "schemaVersion", "selectorVersion", "editorialVersion",
    "date", "generatedAt", "candidateCount", "selectorPoolIdentity",
    "editorialPoolIdentity", "candidates", "provenance",
}
EDITORIAL_REQUIRED = {
    "category", "source", "headline", "summary", "keyTakeaways", "whyItMatters",
    "originalURL", "readTime", "imageURL", "placeTime", "audioURL",
}
EDITION_LEVEL_FIELDS = {"number", "importance", "lead", "focus", "version"}
FORBIDDEN_KEYS = {
    "prompt", "prompts", "rawbody", "raw_body", "articlebody", "article_body",
    "fulltext", "full_text", "sourcetext", "source_text", "providerresponse",
    "provider_response", "apikey", "api_key",
}

READ_TIME_MIN = 1
READ_TIME_MAX = 60
MAX_TAKEAWAYS = 8


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def extract_selector_candidates(artifact: Any) -> list[dict[str, Any]]:
    """The raw selector candidates, in stored order."""
    if not isinstance(artifact, dict) or not isinstance(artifact.get("candidates"), list):
        return []
    return [
        (row.get("selector") or {}) if isinstance(row, dict) else {}
        for row in artifact["candidates"]
    ]


def selector_pool_identity(artifact: Any) -> str:
    """Reuses the RAW identity function, so it equals the original pool's `poolIdentity`."""
    return pool_identity(extract_selector_candidates(artifact))


def editorial_pool_identity(candidates: list[dict[str, Any]]) -> str:
    """Hash of the whole enriched set, ordered by selector id for stability."""
    ordered = sorted(
        candidates,
        key=lambda row: str((row.get("selector") or {}).get("id", ""))
        if isinstance(row, dict) else "",
    )
    return hashlib.sha256(canonical_bytes(ordered)).hexdigest()


def _forbidden_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).lower() in FORBIDDEN_KEYS:
                found.append(child_path)
            found.extend(_forbidden_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{path}[{index}]"))
    elif isinstance(value, str) and (value.startswith("file://") or "/Users/" in value):
        found.append(path)
    return found


def validate_editorial_story(story: Any, prefix: str, errors: list[str]) -> None:
    """Validate one story-level block. Errors carry field paths only — never content."""
    if not isinstance(story, dict):
        errors.append(f"{prefix} must be an object")
        return

    for key in sorted(EDITORIAL_REQUIRED - set(story)):
        errors.append(f"{prefix} missing field: {key}")
    for key in sorted(set(story) - EDITORIAL_REQUIRED):
        errors.append(f"{prefix} unsupported field: {key}")

    for field in ("category", "source", "headline", "summary", "whyItMatters", "placeTime"):
        if not _nonempty(story.get(field)):
            errors.append(f"{prefix}.{field} must be nonempty")

    takeaways = story.get("keyTakeaways")
    if not isinstance(takeaways, list) or not takeaways:
        errors.append(f"{prefix}.keyTakeaways must be a nonempty array")
    else:
        if len(takeaways) > MAX_TAKEAWAYS:
            errors.append(f"{prefix}.keyTakeaways exceeds {MAX_TAKEAWAYS} entries")
        for index, item in enumerate(takeaways):
            if not _nonempty(item):
                errors.append(f"{prefix}.keyTakeaways[{index}] must be nonempty")

    if not _https_url(story.get("originalURL")):
        errors.append(f"{prefix}.originalURL must be an https article URL")
    if not _https_url(story.get("imageURL")):
        errors.append(f"{prefix}.imageURL must be an https URL")

    read_time = story.get("readTime")
    if isinstance(read_time, bool) or not isinstance(read_time, int):
        errors.append(f"{prefix}.readTime must be an integer")
    elif not (READ_TIME_MIN <= read_time <= READ_TIME_MAX):
        errors.append(f"{prefix}.readTime must be within [{READ_TIME_MIN}, {READ_TIME_MAX}]")

    if not isinstance(story.get("audioURL"), str):
        errors.append(f"{prefix}.audioURL must be a string")


def validate_editorial_mix_pool(artifact: Any) -> dict[str, Any]:
    """Validate the whole Editorial Mix Pool artifact."""
    errors: list[str] = []
    if not isinstance(artifact, dict):
        return {"valid": False, "errors": ["artifact must be an object"]}

    for key in sorted(TOP_KEYS - set(artifact)):
        errors.append(f"missing top-level field: {key}")
    for key in sorted(set(artifact) - TOP_KEYS):
        errors.append(f"unsupported top-level field: {key}")

    if artifact.get("artifactType") != ARTIFACT_TYPE:
        errors.append("unsupported artifactType")
    if artifact.get("schemaVersion") != SCHEMA_VERSION:
        errors.append("unsupported schemaVersion")
    if artifact.get("selectorVersion") != SELECTOR_VERSION:
        errors.append("unsupported selectorVersion")
    if artifact.get("editorialVersion") != EDITORIAL_VERSION:
        errors.append("unsupported editorialVersion")

    date = artifact.get("date")
    if not isinstance(date, str) or len(date) != 10 or date[4] != "-" or date[7] != "-":
        errors.append("date must be ISO YYYY-MM-DD")
    if not _nonempty(artifact.get("generatedAt")):
        errors.append("generatedAt must be an ISO datetime")

    candidates = artifact.get("candidates")
    if not isinstance(candidates, list):
        errors.append("candidates must be an array")
        candidates = []
    if artifact.get("candidateCount") != len(candidates):
        errors.append("candidateCount does not match candidates")

    ids: set[str] = set()
    urls: set[str] = set()
    for index, row in enumerate(candidates):
        prefix = f"candidates[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(row) != {"selector", "editorial"}:
            errors.append(f"{prefix} must contain exactly selector and editorial")

        selector = row.get("selector")
        if not isinstance(selector, dict):
            errors.append(f"{prefix}.selector must be an object")
            selector = {}
        else:
            for field in sorted(EDITION_LEVEL_FIELDS & set(selector)):
                errors.append(f"{prefix}.selector must not carry {field}")
            identifier = selector.get("id")
            if not _nonempty(identifier):
                errors.append(f"{prefix}.selector.id must be nonempty")
            elif identifier in ids:
                errors.append(f"duplicate candidate id: {identifier}")
            else:
                ids.add(identifier)
            url = selector.get("url")
            if _nonempty(url):
                if url in urls:
                    errors.append("duplicate candidate url")
                else:
                    urls.add(url)

        editorial = row.get("editorial")
        if not isinstance(editorial, dict):
            errors.append(f"{prefix}.editorial must be an object")
        else:
            for field in sorted(EDITION_LEVEL_FIELDS & set(editorial)):
                errors.append(f"{prefix}.editorial must not carry {field}")
            validate_editorial_story(editorial, f"{prefix}.editorial", errors)
            if editorial.get("category") != selector.get("category"):
                errors.append(f"{prefix}.editorial.category does not match the selector category")
            if _nonempty(selector.get("source")) and editorial.get("source") != selector.get("source"):
                errors.append(f"{prefix}.editorial.source does not match the selector source")

    # Delegate every raw candidate rule to the raw schema via a synthetic raw artifact.
    selectors = extract_selector_candidates(artifact)
    if selectors:
        probe = {
            "schemaVersion": 1,
            "selectorVersion": 1,
            "date": artifact.get("date"),
            "generatedAt": artifact.get("generatedAt"),
            "poolIdentity": pool_identity(selectors),
            "candidateCount": len(selectors),
            "candidates": selectors,
            "validation": {"valid": True, "errors": [], "warnings": []},
            "provenance": {
                "source": "editorial-mix-pool",
                "inputIdentity": "0" * 64,
                "generatorVersion": 1,
            },
        }
        for error in validate_artifact(probe)["errors"]:
            if error.startswith("candidates[") or error.startswith("duplicate "):
                errors.append(f"selector: {error}")

    if artifact.get("selectorPoolIdentity") != pool_identity(selectors):
        errors.append("selectorPoolIdentity does not match the extracted selector candidates")
    if artifact.get("editorialPoolIdentity") != editorial_pool_identity(candidates):
        errors.append("editorialPoolIdentity does not match the enriched candidates")

    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        if not _nonempty(provenance.get("source")):
            errors.append("provenance.source must be nonempty")
        if provenance.get("generatorVersion") != GENERATOR_VERSION:
            errors.append("unsupported provenance.generatorVersion")

    for path in _forbidden_paths(artifact):
        errors.append(f"forbidden field or local path: {path}")

    return {"valid": not errors, "errors": errors}


def freeze_editorial_artifact(
    *,
    date: str,
    generated_at: str,
    candidates: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    """Assemble a canonical Editorial Mix Pool artifact from enriched candidates."""
    ordered = sorted(
        candidates,
        key=lambda row: str((row.get("selector") or {}).get("id", "")),
    )
    artifact = {
        "artifactType": ARTIFACT_TYPE,
        "schemaVersion": SCHEMA_VERSION,
        "selectorVersion": SELECTOR_VERSION,
        "editorialVersion": EDITORIAL_VERSION,
        "date": date,
        "generatedAt": generated_at,
        "candidateCount": len(ordered),
        "selectorPoolIdentity": pool_identity(
            [(row.get("selector") or {}) for row in ordered]
        ),
        "editorialPoolIdentity": editorial_pool_identity(ordered),
        "candidates": ordered,
        "provenance": {"source": source, "generatorVersion": GENERATOR_VERSION},
    }
    return artifact
