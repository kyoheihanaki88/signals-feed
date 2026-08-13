"""Frozen, production-facing schema for offline Custom Mix pool artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

try:
    from .mix_identity import POOL_SELECTOR_VERSION, SUPPORTED_REGIONS, SUPPORTED_TOPICS
except ImportError:
    from mix_identity import POOL_SELECTOR_VERSION, SUPPORTED_REGIONS, SUPPORTED_TOPICS

# Pool artifacts carry the POOL version (their schema is unchanged by selector v2).
SELECTOR_VERSION = POOL_SELECTOR_VERSION

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

# ══════════════════════════════════════════════════════════════════════════════════════
# NUMERIC CONTRACT  (Phase 3D-3A.1)
#
# This is a SEMANTIC SIGNALS SCORE CONTRACT, not a general-purpose JSON-number
# canonicalisation standard. Its only job is to guarantee that every artifact this schema
# accepts has ONE canonical byte representation that a future TypeScript reader can
# reproduce exactly with ordinary `JSON.stringify`.
#
# WHY THE DOMAIN IS DELIBERATELY NARROW. Python and JavaScript disagree on three float
# forms, and the disagreement is invisible until it silently breaks a content hash:
#
#     Python json.dumps        JavaScript JSON.stringify(parsed)
#     7.0      -> "7.0"        7        -> "7"        integral float
#     -0.0     -> "-0.0"       0        -> "0"        negative zero
#     0.000001 -> "1e-06"      1e-6     -> "1e-7"…    exponent notation
#
# JSON text `7.0` parses to `7` in JavaScript with NO surviving trace, so TypeScript can
# never reproduce Python's bytes for an integral float. Rather than write a custom
# serializer or a raw-token parser, the accepted DOMAIN is constrained so that plain
# `json.dumps` already emits the single approved form for every value it accepts.
#
# THE INTEGRAL-FLOAT CASE IS REAL. A 6,272-combination sweep of the actual producer
# (`ranker.base_score` over cluster size × reliability × category × paywall × age ×
# live-blog × importance) yields scores from -7.0 to 17.6 — and -7.0 is an integral float.
# This is not a theoretical hazard.
#
# EXPONENT-SAFE FLOOR. `json.dumps` switches to exponent notation below 1e-4:
# 0.0001 -> "0.0001", but 0.00001 -> "1e-05". A nonzero score must therefore have
# magnitude >= 0.0001. Exact zero is allowed and normalised to the integer 0.
#
# RANGE. The observed producer range is [-7.0, 17.6]. SCORE_MIN/SCORE_MAX give ~57x
# headroom so ordinary ranking changes will not trip the bound, while still rejecting
# absurd values and staying far inside the exponent-safe upper region (|x| < 1e16).
#
# WHERE EACH RULE APPLIES:
#   freeze_artifact   NORMALISES producer output (7.0 -> 7, -0.0 -> 0) and raises on a
#                     value outside the contract. It never silently rounds or clips.
#   validate_artifact REJECTS a non-canonical externally supplied artifact. An artifact
#                     that arrives with 7.0, -0.0, an exponent-domain value or more than
#                     six decimals is invalid — it did not come from this producer.
# The two therefore never disagree: one produces canonical form, the other requires it.
#
# NO CUSTOM SERIALIZER, NO RAW-TOKEN PARSING. Because the accepted domain has exactly one
# canonical form per value, ordinary `json.dumps` already emits it and semantic validation
# is sufficient — there is nothing a JSON tokenizer could catch that the domain does not
# already exclude.
#
# SCHEMA VERSION 1 IS RETAINED. This tightens validation and normalizes previously
# unspecified edge cases; it does not change the serialized shape of any artifact the
# producer already emits. Verified: the pinned real artifact keeps its poolIdentity
# (38d9c03d…), canonical-bytes SHA-256 (41f9bb60…), canonical length (5499) and
# serialize() SHA-256 (2ea69a39…) byte-for-byte. See `test_mix_pool_numeric.py`.
# ══════════════════════════════════════════════════════════════════════════════════════

#: Inclusive bounds for `candidates[].baseScore`. Observed producer range: [-7.0, 17.6].
SCORE_MIN = Decimal("-1000")
SCORE_MAX = Decimal("1000")
#: Maximum fractional decimal digits. The producer emits `round(float(...), 6)`.
SCORE_MAX_DECIMALS = 6
#: Smallest nonzero magnitude `json.dumps` renders without exponent notation.
SCORE_MIN_MAGNITUDE = Decimal("0.0001")

#: Fields that must be exact Python integers, with their inclusive semantic ranges.
#: `sourceRisk` maxes at 7 in `ranker.source_risk` (5 paywalled + 1 unreliable + 1 low).
INTEGRAL_FIELD_RANGES: dict[str, tuple[int, int]] = {
    "candidateCount": (0, 100_000),
    "clusterSize": (1, 100_000),
    "clusterSources": (1, 100_000),
    "sourceRisk": (0, 100),
}
#: Fields that must be real booleans. Listed so `bool` is never mistaken for an integer.
BOOLEAN_FIELDS = {"eligible", "paywalled", "valid"}


class MixPoolNumericError(ValueError):
    """A numeric value outside the Mix Pool contract. Message carries a safe field path."""


def _is_exact_int(value: Any) -> bool:
    """True only for a real `int`. `type(True) is bool`, so booleans are excluded."""
    return type(value) is int


def _score_decimal(value: Any, path: str) -> Decimal:
    """
    Interpret a score through its SHORTEST decimal representation.

    `Decimal(str(0.1))` is `0.1`, whereas `Decimal(0.1)` is the exact binary expansion
    `0.1000000000000000055511151231257827…`. Using `str()` keeps the value the producer
    meant rather than the float's binary noise, which is what makes the 6-decimal limit
    meaningful instead of always failing.
    """
    if isinstance(value, bool):
        raise MixPoolNumericError(f"{path} must be a number, not a boolean")
    if _is_exact_int(value):
        return Decimal(value)
    if not isinstance(value, float):
        raise MixPoolNumericError(f"{path} must be a JSON number")
    if not math.isfinite(value):
        raise MixPoolNumericError(f"{path} must be finite")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - unreachable for finite floats
        raise MixPoolNumericError(f"{path} is not a usable decimal") from exc


def normalize_score(value: Any, path: str = "$.baseScore") -> int | float:
    """
    Return the canonical form of a score, or raise `MixPoolNumericError`.

    Canonical means: an integral value becomes an `int` (so `json.dumps` writes `7`, never
    `7.0`), negative zero becomes `0`, and a fractional value keeps its shortest decimal
    form. The output is asserted against its own serialization before being returned, so
    the guarantee is enforced rather than assumed.
    """
    decimal_value = _score_decimal(value, path)

    if decimal_value < SCORE_MIN or decimal_value > SCORE_MAX:
        raise MixPoolNumericError(
            f"{path} must be within [{SCORE_MIN}, {SCORE_MAX}]"
        )
    exponent = decimal_value.as_tuple().exponent
    decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if decimals > SCORE_MAX_DECIMALS:
        raise MixPoolNumericError(
            f"{path} must have at most {SCORE_MAX_DECIMALS} fractional decimal digits"
        )

    if decimal_value == 0:
        return 0  # covers 0, 0.0 and -0.0
    if abs(decimal_value) < SCORE_MIN_MAGNITUDE:
        raise MixPoolNumericError(
            f"{path} nonzero magnitude must be at least {SCORE_MIN_MAGNITUDE} "
            "(smaller values serialize in exponent notation)"
        )
    if decimal_value == decimal_value.to_integral_value():
        return int(decimal_value)

    normalized = float(decimal_value)
    rendered = json.dumps(normalized)
    if "e" in rendered or "E" in rendered:
        raise MixPoolNumericError(f"{path} must not serialize in exponent notation")
    if rendered.startswith("-0") and Decimal(rendered) == 0:
        raise MixPoolNumericError(f"{path} must not serialize as negative zero")
    if rendered.endswith(".0"):
        raise MixPoolNumericError(f"{path} must not serialize with a trailing .0")
    if Decimal(rendered) != decimal_value:
        raise MixPoolNumericError(f"{path} does not round-trip through JSON")
    return normalized


def validate_integral(value: Any, field: str, path: str, errors: list[str]) -> None:
    """Enforce `type(value) is int` and the field's semantic range."""
    if isinstance(value, bool):
        errors.append(f"{path} must be an integer, not a boolean")
        return
    if not _is_exact_int(value):
        errors.append(f"{path} must be an integer")
        return
    bounds = INTEGRAL_FIELD_RANGES.get(field)
    if bounds and not (bounds[0] <= value <= bounds[1]):
        errors.append(f"{path} must be within [{bounds[0]}, {bounds[1]}]")


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
    validate_integral(
        artifact.get("candidateCount"), "candidateCount", "candidateCount", errors
    )
    if artifact.get("candidateCount") != len(candidates):
        errors.append("candidateCount does not match candidates")
    for field in ("schemaVersion", "selectorVersion"):
        if not _is_exact_int(artifact.get(field)) or isinstance(artifact.get(field), bool):
            errors.append(f"{field} must be an integer")

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
        # An externally supplied artifact must ALREADY be canonical: `7.0`, `-0.0`, an
        # exponent-domain value or over-precision means it did not come from this producer.
        try:
            normalize_score(candidate.get("baseScore"), f"{prefix}.baseScore")
        except MixPoolNumericError as exc:
            errors.append(str(exc))
        else:
            if type(candidate.get("baseScore")) is float and float(
                candidate["baseScore"]
            ).is_integer():
                errors.append(f"{prefix}.baseScore must be an integer, not {candidate['baseScore']!r}")

        quality = candidate.get("quality")
        if quality is not None:
            if not isinstance(quality, dict):
                errors.append(f"{prefix}.quality must be an object")
            else:
                for field in ("clusterSize", "clusterSources", "sourceRisk"):
                    if field in quality:
                        validate_integral(
                            quality[field], field, f"{prefix}.quality.{field}", errors
                        )
                for field in ("eligible", "paywalled"):
                    if field in quality and not isinstance(quality[field], bool):
                        errors.append(f"{prefix}.quality.{field} must be a boolean")
        if "eligible" in candidate and not isinstance(candidate["eligible"], bool):
            errors.append(f"{prefix}.eligible must be a boolean")
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
        validate_integral(
            provenance.get("generatorVersion"),
            "generatorVersion",
            "provenance.generatorVersion",
            errors,
        )
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
        # THE normalization choke point: this runs before `pool_identity`, so the hash is
        # always taken over canonical values. `candidate` is already a deep copy, so the
        # caller's object is never mutated. An out-of-contract producer value raises here
        # rather than being silently rounded or clipped.
        if "baseScore" in candidate:
            candidate["baseScore"] = normalize_score(
                candidate["baseScore"],
                f"candidates[{original.get('id')!s}].baseScore",
            )
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
