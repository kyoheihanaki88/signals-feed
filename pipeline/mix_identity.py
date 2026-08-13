#!/usr/bin/env python3
"""Canonical identities for deterministic Signals Custom Mix selection."""

from __future__ import annotations

import re
from typing import Iterable

# SELECTION semantics version. v2 (2026-08-13): selected topics are a STRICT ALLOWLIST
# (an unselected topic is never chosen, not even by fallback), and regions fill in the
# fixed priority united_states > japan > world with a minimum of 3 US stories when the
# US is selected and its pool suffices. Bumped so every cached mix / identity computed
# under the v1 "topic boost" semantics is invalidated rather than reused.
SELECTOR_VERSION = 2
# CANDIDATE POOL artifact version. The pool is the selector's INPUT; its schema did not
# change in v2, so published pool artifacts stay valid and keep this version. Deliberately
# decoupled from SELECTOR_VERSION above.
POOL_SELECTOR_VERSION = 1
SUPPORTED_REGIONS = ("japan", "united_states", "world")
SUPPORTED_TOPICS = ("ai", "business", "climate", "culture", "health", "science", "tech")


class UnsupportedMixValue(ValueError):
    """Raised when a persisted/display value cannot map to a supported canonical ID."""


def _key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[.\s-]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


_REGION_ALIASES = {
    "japan": "japan",
    "jp": "japan",
    "jpn": "japan",
    "us": "united_states",
    "usa": "united_states",
    "u_s": "united_states",
    "united_states": "united_states",
    "world": "world",
}
_TOPIC_ALIASES = {topic: topic for topic in SUPPORTED_TOPICS}


def _normalize(values: Iterable[object], aliases: dict[str, str], kind: str) -> tuple[str, ...]:
    canonical = set()
    unsupported = []
    for value in values or ():
        key = _key(value)
        if key in aliases:
            canonical.add(aliases[key])
        else:
            unsupported.append(str(value))
    if unsupported:
        raise UnsupportedMixValue(f"unsupported {kind}: {', '.join(sorted(unsupported))}")
    return tuple(sorted(canonical))


def normalize_regions(values: Iterable[object]) -> tuple[str, ...]:
    return _normalize(values, _REGION_ALIASES, "region")


def normalize_topics(values: Iterable[object]) -> tuple[str, ...]:
    return _normalize(values, _TOPIC_ALIASES, "topic")


def normalize_mix(regions: Iterable[object], topics: Iterable[object]) -> dict[str, tuple[str, ...]]:
    return {"regions": normalize_regions(regions), "topics": normalize_topics(topics)}


def mix_identity(date: str, regions: Iterable[object], topics: Iterable[object],
                 selector_version: int = SELECTOR_VERSION, size: int = 5) -> str:
    normalized = normalize_mix(regions, topics)
    return (
        f"date={str(date).strip()}|regions={','.join(normalized['regions'])}"
        f"|topics={','.join(normalized['topics'])}|selector={int(selector_version)}"
        f"|size={int(size)}"
    )
