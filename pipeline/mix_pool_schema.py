"""Frozen, production-facing schema for offline Custom Mix pool artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

try:
    from .mix_identity import SELECTOR_VERSION, SUPPORTED_REGIONS, SUPPORTED_TOPICS
except ImportError:
    from mix_identity import SELECTOR_VERSION, SUPPORTED_REGIONS, SUPPORTED_TOPICS

SCHEMA_VERSION = 1
GENERATOR_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION}
SUPPORTED_SELECTOR_VERSIONS = {SELECTOR_VERSION}
STRENGTHS = {"primary", "incidental", "none"}
SOURCE_RELIABILITIES = {"high", "medium", "low", "unknown"}
CATEGORIES = {
    "AI", "BUSINESS", "CLIMATE", "CULTURE", "ECONOMY", "FINANCE",
    "HEALTH", "JAPAN", "SCIENCE", "TECH", "WORLD", "OTHER",
}
FORBIDDEN_KEYS = {
    "rawbody", "raw_body", "fulltext", "full_text", "audiodata",
    "audio_data", "authtoken", "auth_token",
}
TOP_KEYS = {
    "schemaVersion", "selectorVersion", "date", "generatedAt", "poolIdentity",
    "candidateCount", "candidates", "validation", "provenance",
}
CANDIDATE_REQUIRED = {
    "id", "headline", "summary", "source", "url", "publishedAt", "category",
    "topics", "regionMemberships", "baseScore", "sourceReliability",
    "topicFingerprint", "underlyingStoryIdentity",
}
CANDIDATE_ALLOWED = CANDIDATE_REQUIRED | {"quality", "eligible"}
PROVENANCE_KEYS = {"source", "inputIdentity", "generatorVersion", "referenceAt"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def serialize(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def input_identity(source: dict[str, Any]) -> str:
    normalized = copy.deepcopy(source)
    candidates = normalized.get("candidates")
    if isinstance(candidates, list):
        normalized["candidates"] = sorted(
            candidates,
            key=lambda row: (
                str(row.get("id", "")),
                str(row.get("canonical_url") or row.get("url", "")),
            ),
        )
    return hashlib.sha256(canonical_bytes(normalized)).hexdigest()


def pool_identity(candidates: list[dict[str, Any]]) -> str:
    ordered = sorted(candidates, key=lambda row: str(row.get("id", "")))
    return hashlib.sha256(canonical_bytes(ordered)).hexdigest()


def _iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def validate_artifact(artifact: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(artifact, dict):
        return {"valid": False, "errors": ["artifact must be an object"], "warnings": []}

    missing = TOP_KEYS - set(artifact)
    extra = set(artifact) - TOP_KEYS
    errors += [f"missing top-level field: {key}" for key in sorted(missing)]
    errors += [f"unsupported top-level field: {key}" for key in sorted(extra)]
    if artifact.get("schemaVersion") not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append("unsupported schemaVersion")
    if artifact.get("selectorVersion") not in SUPPORTED_SELECTOR_VERSIONS:
        errors.append("unsupported selectorVersion")
    date = artifact.get("date")
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        errors.append("date must be ISO YYYY-MM-DD")
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            errors.append("date must be a real calendar date")
    if not _iso_datetime(artifact.get("generatedAt")):
        errors.append("generatedAt must be an ISO datetime")

    candidates = artifact.get("candidates")
    if not isinstance(candidates, list):
        errors.append("candidates must be an array")
        candidates = []
    if artifact.get("candidateCount") != len(candidates):
        errors.append("candidateCount does not match candidates")

    ids: set[str] = set()
    urls: set[str] = set()
    for index, candidate in enumerate(candidates):
        prefix = f"candidates[{index}]"
        if not isinstance(candidate, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in sorted(CANDIDATE_REQUIRED - set(candidate)):
            errors.append(f"{prefix} missing field: {key}")
        for key in sorted(set(candidate) - CANDIDATE_ALLOWED):
            errors.append(f"{prefix} unsupported field: {key}")
        identifier = candidate.get("id")
        if not _nonempty(identifier):
            errors.append(f"{prefix}.id must be nonempty")
        elif identifier in ids:
            errors.append(f"duplicate candidate id: {identifier}")
        else:
            ids.add(identifier)
        url = candidate.get("url")
        if not _http_url(url):
            errors.append(f"{prefix}.url must be http(s)")
        elif url in urls:
            errors.append(f"duplicate candidate url: {url}")
        else:
            urls.add(url)
        for field in ("headline", "summary", "source", "underlyingStoryIdentity"):
            if not _nonempty(candidate.get(field)):
                errors.append(f"{prefix}.{field} must be nonempty")
        fingerprint = candidate.get("topicFingerprint")
        if not isinstance(fingerprint, (str, list)):
            errors.append(f"{prefix}.topicFingerprint must be a string or array")
        elif isinstance(fingerprint, list) and any(not _nonempty(item) for item in fingerprint):
            errors.append(f"{prefix}.topicFingerprint entries must be nonempty")
        if not _iso_datetime(candidate.get("publishedAt")):
            errors.append(f"{prefix}.publishedAt must be an ISO datetime")
        if candidate.get("category") not in CATEGORIES:
            errors.append(f"{prefix}.category is not canonical")
        topics = candidate.get("topics")
        if not isinstance(topics, list) or any(topic not in SUPPORTED_TOPICS for topic in topics):
            errors.append(f"{prefix}.topics contains a noncanonical topic")
        elif len(topics) != len(set(topics)):
            errors.append(f"{prefix}.topics contains duplicates")
        for field in ("baseScore",):
            value = candidate.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                errors.append(f"{prefix}.{field} must be finite")
        if candidate.get("sourceReliability") not in SOURCE_RELIABILITIES:
            errors.append(f"{prefix}.sourceReliability is not allowed")
        memberships = candidate.get("regionMemberships")
        if not isinstance(memberships, list):
            errors.append(f"{prefix}.regionMemberships must be an array")
            continue
        region_ids: set[str] = set()
        for member_index, membership in enumerate(memberships):
            mp = f"{prefix}.regionMemberships[{member_index}]"
            if not isinstance(membership, dict):
                errors.append(f"{mp} must be an object")
                continue
            if set(membership) != {"id", "strength", "evidence"}:
                errors.append(f"{mp} must contain only id, strength, evidence")
            region = membership.get("id")
            if region not in SUPPORTED_REGIONS:
                errors.append(f"{mp}.id is not canonical")
            elif region in region_ids:
                errors.append(f"{prefix} has duplicate region membership: {region}")
            else:
                region_ids.add(region)
            strength = membership.get("strength")
            if strength not in STRENGTHS:
                errors.append(f"{mp}.strength is not allowed")
            evidence = membership.get("evidence")
            if not isinstance(evidence, list) or any(not _nonempty(item) for item in evidence):
                errors.append(f"{mp}.evidence must contain nonempty strings")
            if strength == "primary" and not evidence:
                errors.append(f"{mp} primary membership requires evidence")

    expected_identity = pool_identity(candidates)
    if artifact.get("poolIdentity") != expected_identity:
        errors.append("poolIdentity does not match candidates")

    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        if set(provenance) - PROVENANCE_KEYS:
            errors.append("provenance contains unsupported fields")
        for field in ("source", "inputIdentity"):
            if not _nonempty(provenance.get(field)):
                errors.append(f"provenance.{field} must be nonempty")
        if provenance.get("generatorVersion") != GENERATOR_VERSION:
            errors.append("unsupported provenance.generatorVersion")
        if "referenceAt" in provenance and not _iso_datetime(provenance["referenceAt"]):
            errors.append("provenance.referenceAt must be an ISO datetime")

    embedded = artifact.get("validation")
    if not isinstance(embedded, dict) or set(embedded) != {"valid", "errors", "warnings"}:
        errors.append("validation must contain valid, errors, warnings")
    elif not isinstance(embedded.get("valid"), bool) or not isinstance(
        embedded.get("errors"), list
    ) or not isinstance(embedded.get("warnings"), list):
        errors.append("validation fields have invalid types")

    for path in _forbidden_paths(artifact):
        errors.append(f"forbidden field or local path: {path}")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def freeze_artifact(
    pool: dict[str, Any],
    *,
    source_input: dict[str, Any],
    source: str,
    selector_version: int = SELECTOR_VERSION,
    reference_at: str | None = None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for original in pool.get("candidates", []):
        candidate = {
            key: copy.deepcopy(value)
            for key, value in original.items()
            if key in CANDIDATE_ALLOWED and key != "regionMemberships"
        }
        candidate["regionMemberships"] = sorted(
            [
                {
                    "id": row.get("id") or row.get("region"),
                    "strength": row.get("strength"),
                    "evidence": sorted(row.get("evidence", [])),
                }
                for row in original.get("regionMemberships", [])
            ],
            key=lambda row: str(row["id"]),
        )
        candidate["topics"] = sorted(candidate.get("topics", []))
        candidates.append(candidate)
    candidates.sort(key=lambda row: str(row.get("id", "")))
    provenance: dict[str, Any] = {
        "source": source,
        "inputIdentity": input_identity(source_input),
        "generatorVersion": GENERATOR_VERSION,
    }
    if reference_at is not None:
        provenance["referenceAt"] = reference_at
    artifact = {
        "schemaVersion": SCHEMA_VERSION,
        "selectorVersion": selector_version,
        "date": pool.get("date"),
        "generatedAt": pool.get("generatedAt"),
        "poolIdentity": pool_identity(candidates),
        "candidateCount": len(candidates),
        "candidates": candidates,
        "validation": {"valid": True, "errors": [], "warnings": []},
        "provenance": provenance,
    }
    artifact["validation"] = validate_artifact(artifact)
    return artifact
