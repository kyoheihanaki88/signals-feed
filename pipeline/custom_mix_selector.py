#!/usr/bin/env python3
"""Fixture-first deterministic Custom Mix selector. No network, LLM, or publishing."""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit, urlunsplit

from editorial import duplicate_story
from mix_identity import SELECTOR_VERSION, mix_identity, normalize_mix, normalize_regions
from region_classifier import classify_regions

TOPIC_ADJUSTMENT = 10.0
NEW_CATEGORY_BONUS = 0.6
NEW_SOURCE_BONUS = 0.4

# ── selector v2 (2026-08-13) ────────────────────────────────────────────────────────────
# Selected topics are a STRICT ALLOWLIST: a candidate whose topics do not intersect the
# selection is ineligible for EVERY phase, including global fallback. Rather than fill a
# slot with an article that violates the user's settings, the mix ships short (fail
# closed; `shortage`/`unfilledSlots` report it). An empty topic selection means "no topic
# filter" — the pre-v2 behavior.
#
# Regions fill in a fixed priority order. A UK story is a "world" story (the classifier
# gives it world membership) and therefore competes only for world slots — it can never
# displace a united_states slot.
REGION_PRIORITY = ("united_states", "japan", "world")
# With the US selected (alongside others) and enough distinct US stories, at least 3 of
# the 5 slots are US stories.
US_MIN_QUOTA = 3


def _priority_order(regions) -> tuple[str, ...]:
    return tuple(r for r in REGION_PRIORITY if r in regions)


def _topic_allowed(candidate: dict, topics: tuple[str, ...]) -> bool:
    """v2 strict allowlist. Empty selection = no filter (unchanged behavior)."""
    if not topics:
        return True
    candidate_topics = {str(v).lower() for v in candidate.get("topics", [])}
    return bool(candidate_topics.intersection(topics))


def _memberships(candidate: dict) -> dict[str, str]:
    rows = candidate.get("regionMemberships")
    if rows is None:
        rows = classify_regions(candidate)
    result = {}
    for row in rows or []:
        region = row.get("region") or row.get("id")
        if region:
            try:
                canonical = normalize_regions([region])[0]
            except ValueError:
                continue
            result[canonical] = str(row.get("strength", "none"))
    return result


def _canonical_url(url: str) -> str:
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))
    except Exception:
        return str(url or "")


def _eligible(candidate: dict, edition_date: str) -> tuple[bool, str]:
    if candidate.get("eligible") is False:
        return False, "candidate marked ineligible"
    if str(candidate.get("sourceReliability", "")).lower() == "low":
        return False, "low source reliability"
    if not candidate.get("id") or not candidate.get("headline") or not candidate.get("source"):
        return False, "missing required field"
    if not str(candidate.get("url", "")).startswith(("https://", "http://")):
        return False, "invalid article URL"
    try:
        published = dt.datetime.fromisoformat(
            str(candidate.get("publishedAt", "")).replace("Z", "+00:00")
        )
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.timezone.utc)
        edition_end = dt.datetime.fromisoformat(str(edition_date)).replace(
            hour=23, minute=59, second=59, tzinfo=dt.timezone.utc
        )
        age = edition_end - published.astimezone(dt.timezone.utc)
        if age > dt.timedelta(hours=72) or age < dt.timedelta(hours=-24):
            return False, "outside 72-hour freshness window"
    except (TypeError, ValueError):
        return False, "invalid publishedAt"
    return True, ""


def _duplicate(candidate: dict, selected: list[dict]) -> tuple[bool, str]:
    identity = candidate.get("underlyingStoryIdentity")
    url = _canonical_url(candidate.get("url", ""))
    for other in selected:
        if identity and identity == other.get("underlyingStoryIdentity"):
            return True, f"duplicate underlyingStoryIdentity={identity}"
        if url and url == _canonical_url(other.get("url", "")):
            return True, "duplicate canonical URL"
        dup, reason, rule = duplicate_story(
            candidate.get("headline", ""), candidate.get("summary", ""),
            other.get("headline", ""), other.get("summary", ""),
        )
        if dup:
            return True, f"existing duplicate guard: {rule}: {reason}"
    return False, ""


def _topic_adjustment(candidate: dict, topics: tuple[str, ...]) -> float:
    candidate_topics = {str(v).lower() for v in candidate.get("topics", [])}
    return TOPIC_ADJUSTMENT * len(candidate_topics.intersection(topics))


def _rank(candidate: dict, topics: tuple[str, ...], selected: list[dict]) -> tuple:
    base = float(candidate.get("baseScore", 0))
    adjustment = _topic_adjustment(candidate, topics)
    categories = {str(c.get("category", "")).lower() for c in selected}
    sources = {str(c.get("source", "")).lower() for c in selected}
    diversity = 0.0
    if str(candidate.get("category", "")).lower() not in categories:
        diversity += NEW_CATEGORY_BONUS
    if str(candidate.get("source", "")).lower() not in sources:
        diversity += NEW_SOURCE_BONUS
    final = base + adjustment + diversity
    return (-final, str(candidate.get("id"))), base, adjustment, final


def _pick(pool: list[dict], count: int, topics: tuple[str, ...], selected: list[dict],
          phase: str, logs: dict[str, dict]) -> list[dict]:
    picked = []
    remaining = list(pool)
    while remaining and len(picked) < count:
        ranked = sorted((_rank(c, topics, selected + picked)[0], c) for c in remaining)
        _, candidate = ranked[0]
        remaining.remove(candidate)
        duplicate, reason = _duplicate(candidate, selected + picked)
        _, base, adjustment, final = _rank(candidate, topics, selected + picked)
        log = logs[candidate["id"]]
        log.update({"baseScore": base, "topicAdjustment": adjustment,
                    "finalScore": final, "selectionPhase": phase})
        if duplicate:
            log["rejectionReason"] = reason
            continue
        log["rejectionReason"] = None
        picked.append(candidate)
    return picked


def _initial_targets(regions: tuple[str, ...], candidates: list[dict], size: int,
                     identity: str) -> dict[str, int]:
    """v2 quotas: fixed priority united_states > japan > world.

    With the US selected alongside other regions it takes US_MIN_QUOTA slots up front;
    the rest split evenly over the remaining regions with any remainder awarded in
    priority order. Without the US, slots split evenly with the remainder in priority
    order. (v1's strength/hash remainder is gone — the order is part of the spec now.)
    `candidates`/`identity` are kept in the signature for call-site compatibility."""
    ordered = _priority_order(regions)
    targets = {region: 0 for region in regions}
    if "united_states" in ordered and len(ordered) > 1:
        us = min(US_MIN_QUOTA, size)
        targets["united_states"] = us
        others = tuple(r for r in ordered if r != "united_states")
        base, remainder = divmod(size - us, len(others))
        for region in others:
            targets[region] = base
        for region in others[:remainder]:
            targets[region] += 1
    else:
        base, remainder = divmod(size, len(ordered))
        for region in ordered:
            targets[region] = base
        for region in ordered[:remainder]:
            targets[region] += 1
    return targets


def _dedup_count(pool: list[dict]) -> int:
    """Count the deterministic unique-story pool without changing selection logs."""
    unique = []
    for candidate in sorted(pool, key=lambda c: (-float(c.get("baseScore", 0)),
                                                  str(c.get("id", "")))):
        duplicate, _ = _duplicate(candidate, unique)
        if not duplicate:
            unique.append(candidate)
    return len(unique)


def select_custom_mix(candidates: list[dict], date: str, regions, topics=(),
                      size: int = 5, selector_version: int = SELECTOR_VERSION) -> dict:
    normalized = normalize_mix(regions, topics)
    selected_regions = normalized["regions"]
    selected_topics = normalized["topics"]
    if not selected_regions:
        raise ValueError("at least one region is required")
    identity = mix_identity(date, selected_regions, selected_topics, selector_version, size)

    logs = {}
    eligible = []
    for candidate in sorted(candidates, key=lambda c: str(c.get("id", ""))):
        ok, reason = _eligible(candidate, date)
        # v2 strict topic allowlist: an unselected topic is ineligible for EVERY phase,
        # fallback included — checked after the base checks so their reasons win.
        if ok and not _topic_allowed(candidate, selected_topics):
            ok, reason = False, "topic not selected (strict allowlist)"
        memberships = _memberships(candidate)
        region_eligible = [r for r in selected_regions if memberships.get(r) == "primary"]
        logs[str(candidate.get("id", ""))] = {
            "id": candidate.get("id"),
            "baseScore": float(candidate.get("baseScore", 0)),
            "topicAdjustment": _topic_adjustment(candidate, selected_topics),
            "regionEligibility": region_eligible,
            "finalScore": None,
            "selectionPhase": None,
            "rejectionReason": None if ok else reason,
        }
        if ok:
            eligible.append(candidate)

    region_pool = [c for c in eligible
                   if any(_memberships(c).get(r) == "primary" for r in selected_regions)]
    selected = []
    assigned_regions = []

    if len(selected_regions) == 1:
        region = selected_regions[0]
        picked = _pick(
            [c for c in region_pool if _memberships(c).get(region) == "primary"],
            size, selected_topics, selected, "regional_primary", logs,
        )
        selected.extend(picked)
        assigned_regions.extend([region] * len(picked))
    else:
        targets = _initial_targets(selected_regions, region_pool, size, identity)
        # Quotas fill in PRIORITY order (US first), so the US takes its slots before any
        # lower-priority region can consume a story that also has US membership.
        for region in _priority_order(selected_regions):
            picked = _pick(
                [c for c in region_pool if _memberships(c).get(region) == "primary"
                 and c not in selected],
                targets[region], selected_topics, selected, f"regional_quota:{region}", logs,
            )
            selected.extend(picked)
            assigned_regions.extend([region] * len(picked))
        # Deterministic reallocation, still inside the selected-region scope, and still in
        # priority order: an unmet quota is refilled from the US pool first, then japan,
        # then world — a deep US pool grows the US share, never the other way around.
        if len(selected) < size:
            for region in _priority_order(selected_regions):
                if len(selected) >= size:
                    break
                picked = _pick(
                    [c for c in region_pool if _memberships(c).get(region) == "primary"
                     and c not in selected],
                    size - len(selected), selected_topics, selected,
                    f"regional_reallocation:{region}", logs,
                )
                selected.extend(picked)
                assigned_regions.extend([region] * len(picked))

    regional_count = len(selected)
    fallback_slots = 0
    if len(selected) < size:
        fallback = [c for c in eligible if c not in selected]
        picked = _pick(fallback, size - len(selected), (), selected, "global_fallback", logs)
        selected.extend(picked)
        fallback_slots = len(picked)
        assigned_regions.extend(["global_fallback"] * len(picked))

    # A final guard independent from each selection phase.
    for i, candidate in enumerate(selected):
        duplicate, reason = _duplicate(candidate, selected[:i])
        if duplicate:
            raise AssertionError(f"final duplicate guard failed: {candidate['id']}: {reason}")

    qualifying = len(region_pool)
    regional_after_dedup = _dedup_count(region_pool)
    shortage = len(selected) < size
    if shortage:
        fallback_reason = "insufficient total eligible candidates"
    elif fallback_slots:
        fallback_reason = "insufficient qualifying regional candidates"
    else:
        fallback_reason = None

    final_mix = {}
    for region in assigned_regions:
        final_mix[region] = final_mix.get(region, 0) + 1
    for candidate in selected:
        log = logs[candidate["id"]]
        if log["selectionPhase"] is None:
            log["selectionPhase"] = "selected"
    for candidate in eligible:
        log = logs[candidate["id"]]
        if log["finalScore"] is None:
            log["finalScore"] = float(candidate.get("baseScore", 0)) + log["topicAdjustment"]
        if log["selectionPhase"] is None:
            if not log["regionEligibility"]:
                log["selectionPhase"] = "outside_scope"
                log["rejectionReason"] = "not primary for any selected region"
            else:
                log["selectionPhase"] = "regional_primary"
                log["rejectionReason"] = "lower deterministic rank after requested slots filled"

    return {
        "selectedIds": [c["id"] for c in selected],
        "metadata": {
            "selectedRegions": list(selected_regions),
            "selectedTopics": list(selected_topics),
            "requestedRegionCount": size,
            "candidatePoolTotal": len(candidates),
            "qualifyingRegionCandidates": qualifying,
            "regionalCandidatesAfterDedup": regional_after_dedup,
            "selectedRegionStories": regional_count,
            "fallbackSlots": fallback_slots,
            "fallbackReason": fallback_reason,
            "selectorVersion": selector_version,
            "mixIdentity": identity,
            "finalRegionMix": final_mix,
            "shortage": shortage,
            "unfilledSlots": max(0, size - len(selected)),
        },
        "candidateLogs": [logs[k] for k in sorted(logs)],
    }
