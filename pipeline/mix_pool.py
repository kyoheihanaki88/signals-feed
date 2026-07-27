#!/usr/bin/env python3
"""Build a deterministic Custom Mix candidate pool from Scout output.

This module is deliberately offline and unconnected to production workflows. It
does not fetch, draft, localize, publish, or overwrite an existing artifact.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit

import ranker
from editorial import topic_fingerprint
from region_classifier import classify_regions

POOL_SCHEMA_VERSION = 1

_CATEGORY_TOPICS = {
    "AI": ("ai", "tech"),
    "BUSINESS": ("business",),
    "CLIMATE": ("climate",),
    "CULTURE": ("culture",),
    "ECONOMY": ("business",),
    "FINANCE": ("business",),
    "HEALTH": ("health",),
    "SCIENCE": ("science",),
    "TECH": ("tech",),
}
_TEXT_TOPICS = {
    "ai": re.compile(r"\b(ai|artificial intelligence|machine learning)\b", re.I),
    "business": re.compile(r"\b(business|economy|economic|finance|market|company)\b", re.I),
    "climate": re.compile(r"\b(climate|emissions?|warming|wildfires?|drought)\b", re.I),
    "culture": re.compile(r"\b(culture|museum|art|heritage|literature)\b", re.I),
    "health": re.compile(r"\b(health|hospital|medical|disease|vaccine)\b", re.I),
    "science": re.compile(r"\b(science|research|scientists?|laborator(?:y|ies))\b", re.I),
    "tech": re.compile(r"\b(tech|technology|software|chip|semiconductor|robot)\b", re.I),
}


class MixPoolError(ValueError):
    """Raised when source data cannot produce a trustworthy deterministic pool."""


def _parse_time(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MixPoolError(f"invalid {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.strip("/"):
        return ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def _candidate_id(candidate: dict) -> str:
    canonical = _canonical_url(candidate.get("canonical_url") or candidate.get("url"))
    return str(candidate.get("id") or hashlib.sha1(canonical.encode()).hexdigest()[:6])


def _topics(candidate: dict) -> list[str]:
    result = set(_CATEGORY_TOPICS.get(str(candidate.get("category", "")).upper(), ()))
    text = f"{candidate.get('title', '')} {candidate.get('snippet', '')}"
    result.update(topic for topic, pattern in _TEXT_TOPICS.items() if pattern.search(text))
    return sorted(result)


def _story_identity(candidate: dict) -> str:
    cluster = candidate.get("cluster_id")
    if cluster is not None and int(candidate.get("cluster_size") or 1) > 1:
        return f"cluster:{cluster}"
    canonical = _canonical_url(candidate.get("canonical_url") or candidate.get("url"))
    return "url:" + hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _exclusion_reason(candidate: dict, now: dt.datetime, stale_hours: int) -> str | None:
    if not ranker.has_real_url(candidate.get("url", "")):
        return "invalid_article_url"
    if ranker.is_stale(candidate, now, stale_hours):
        return "stale"
    if not ranker.resolvable(candidate):
        return "unresolvable_candidate_id"
    if not ranker.likely_complete(candidate):
        return "thin_or_paywalled_source"
    kind = ranker.editorial_kind(candidate)
    if kind:
        return f"editorial_exclusion:{kind}"
    return None


def _normalize_candidate(candidate: dict, now: dt.datetime, fresh_hours: int) -> dict:
    canonical = _canonical_url(candidate.get("canonical_url") or candidate.get("url"))
    classifier_input = {
        "title": candidate.get("title", ""),
        "summary": candidate.get("snippet", ""),
        "metadata": candidate.get("metadata", {}),
        "structuredRegions": (
            candidate.get("structuredRegions") or candidate.get("structured_regions") or []
        ),
    }
    fingerprint = sorted(topic_fingerprint(
        candidate.get("title", ""), candidate.get("snippet", "")
    ))
    return {
        "id": _candidate_id(candidate),
        "headline": str(candidate.get("title", "")).strip(),
        "summary": str(candidate.get("snippet", "")).strip(),
        "source": str(candidate.get("source", "")).strip(),
        "url": canonical,
        "publishedAt": _parse_time(candidate.get("published_at"), "published_at")
        .isoformat()
        .replace("+00:00", "Z"),
        "category": str(candidate.get("category") or "OTHER").upper(),
        "topics": _topics(candidate),
        "regionMemberships": classify_regions(classifier_input),
        "baseScore": round(float(ranker.base_score(candidate, now, fresh_hours)), 6),
        "sourceReliability": str(candidate.get("source_reliability") or "unknown").lower(),
        "topicFingerprint": fingerprint,
        "underlyingStoryIdentity": _story_identity(candidate),
        "quality": {
            "eligible": True,
            "paywalled": bool(candidate.get("paywalled")),
            "sourceRisk": ranker.source_risk(candidate),
            "clusterSize": int(candidate.get("cluster_size") or 1),
            "clusterSources": int(candidate.get("cluster_sources") or 1),
        },
        "eligible": True,
    }


def validate_mix_pool(pool: dict) -> None:
    errors = []
    if pool.get("schemaVersion") != POOL_SCHEMA_VERSION:
        errors.append("unsupported schemaVersion")
    try:
        dt.date.fromisoformat(str(pool.get("date")))
    except ValueError:
        errors.append("date must be YYYY-MM-DD")
    try:
        _parse_time(pool.get("generatedAt"), "generatedAt")
    except MixPoolError as exc:
        errors.append(str(exc))

    candidates = pool.get("candidates")
    if not isinstance(candidates, list):
        errors.append("candidates must be a list")
        candidates = []
    if pool.get("candidateCount") != len(candidates):
        errors.append("candidateCount does not match candidates")
    ids, urls = set(), set()
    required = (
        "id", "headline", "summary", "source", "url", "publishedAt", "category",
        "topics", "regionMemberships", "baseScore", "sourceReliability",
        "topicFingerprint", "underlyingStoryIdentity",
    )
    for index, candidate in enumerate(candidates):
        missing = [key for key in required if key not in candidate]
        if missing:
            errors.append(f"candidate[{index}] missing {','.join(missing)}")
            continue
        if candidate["id"] in ids:
            errors.append(f"duplicate candidate id: {candidate['id']}")
        ids.add(candidate["id"])
        if candidate["url"] in urls:
            errors.append(f"duplicate canonical URL: {candidate['url']}")
        urls.add(candidate["url"])
        if not _canonical_url(candidate["url"]):
            errors.append(f"candidate[{index}] invalid URL")
        if not candidate["headline"] or not candidate["summary"] or not candidate["source"]:
            errors.append(f"candidate[{index}] empty required copy")
        try:
            _parse_time(candidate["publishedAt"], "publishedAt")
        except MixPoolError as exc:
            errors.append(f"candidate[{index}] {exc}")
    if errors:
        raise MixPoolError("; ".join(errors))


def build_mix_pool(source: dict, date: str, generated_at: str, *,
                   now: str | None = None, stale_hours: int = 48,
                   fresh_hours: int = 36) -> dict:
    try:
        dt.date.fromisoformat(str(date))
    except ValueError as exc:
        raise MixPoolError(f"invalid date: {date!r}") from exc
    generated = _parse_time(generated_at, "generatedAt")
    reference = _parse_time(now or generated_at, "now")
    rows = source.get("candidates")
    if not isinstance(rows, list):
        raise MixPoolError("source candidates must be a list")

    candidates, excluded = [], []
    seen_source_ids = {}
    for original in rows:
        candidate = dict(original)
        canonical = _canonical_url(candidate.get("canonical_url") or candidate.get("url"))
        candidate["canonical_url"] = canonical
        candidate["id"] = _candidate_id(candidate)
        prior = seen_source_ids.get(candidate["id"])
        if prior and prior != canonical:
            raise MixPoolError(f"candidate id collision: {candidate['id']}")
        seen_source_ids[candidate["id"]] = canonical
        reason = _exclusion_reason(candidate, reference, stale_hours)
        if reason:
            excluded.append({"id": candidate["id"], "reason": reason})
            continue
        candidates.append(_normalize_candidate(candidate, reference, fresh_hours))

    candidates.sort(key=lambda row: row["id"])
    excluded.sort(key=lambda row: (row["id"], row["reason"]))
    digest_payload = json.dumps(candidates, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode()
    pool = {
        "schemaVersion": POOL_SCHEMA_VERSION,
        "date": str(date),
        "generatedAt": generated.isoformat().replace("+00:00", "Z"),
        "sourceCandidateCount": len(rows),
        "candidateCount": len(candidates),
        "excludedCount": len(excluded),
        "poolIdentity": hashlib.sha256(digest_payload).hexdigest(),
        "candidates": candidates,
        "excludedCandidates": excluded,
    }
    validate_mix_pool(pool)
    return pool


def write_mix_pool(path: str, pool: dict) -> None:
    """Atomically create a pool artifact; immutable means an existing path is an error."""
    validate_mix_pool(pool)
    if os.path.exists(path):
        raise FileExistsError(f"refusing to overwrite existing mix pool: {path}")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mix-pool-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pool, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an offline deterministic Custom Mix pool")
    parser.add_argument("--input", required=True, help="Scout candidates JSON")
    parser.add_argument("--out", required=True, help="new immutable pool path")
    parser.add_argument("--date", required=True, help="edition date YYYY-MM-DD")
    parser.add_argument("--generated-at", required=True, help="explicit ISO timestamp")
    parser.add_argument("--now", help="explicit scoring reference time (default: generated-at)")
    parser.add_argument("--stale-hours", type=int, default=48)
    parser.add_argument("--fresh-hours", type=int, default=36)
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        source = json.load(handle)
    pool = build_mix_pool(source, args.date, args.generated_at, now=args.now,
                          stale_hours=args.stale_hours, fresh_hours=args.fresh_hours)
    write_mix_pool(args.out, pool)
    print(
        f"wrote {args.out}: candidates={pool['candidateCount']} "
        f"excluded={pool['excludedCount']} identity={pool['poolIdentity'][:12]}"
    )


if __name__ == "__main__":
    main()
