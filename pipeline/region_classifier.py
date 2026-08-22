#!/usr/bin/env python3
"""Deterministic, evidence-bearing geographic classification for Custom Mix."""

from __future__ import annotations

import re

from mix_identity import normalize_regions

_COMPANIES = re.compile(r"\b(sony|toyota|nintendo|softbank|honda)\b", re.I)

_JAPAN = (
    ("japan", re.compile(r"\bjapan\b", re.I)),
    ("japanese", re.compile(r"\bjapanese\b", re.I)),
    ("tokyo", re.compile(r"\btokyo\b", re.I)),
    ("osaka", re.compile(r"\bosaka\b", re.I)),
    ("kyoto", re.compile(r"\bkyoto\b", re.I)),
    ("okinawa", re.compile(r"\bokinawa\b", re.I)),
    ("hokkaido", re.compile(r"\bhokkaido\b", re.I)),
    ("fukushima", re.compile(r"\bfukushima\b", re.I)),
    ("yen", re.compile(r"\byen\b", re.I)),
    ("bank-of-japan", re.compile(r"\bbank of japan\b|\bboj\b", re.I)),
    ("nikkei", re.compile(r"\bnikkei\b", re.I)),
    ("japanese-government", re.compile(r"\bjapanese government\b", re.I)),
)
_US = (
    ("united-states", re.compile(r"\bunited states\b|\bu\.s\.\b|\busa\b", re.I)),
    ("washington", re.compile(r"\bwashington(?:,\s*d\.?c\.?)?\b", re.I)),
    ("white-house", re.compile(r"\bwhite house\b", re.I)),
    ("congress", re.compile(r"\bcongress\b", re.I)),
    # 2026-08-18 audit additions — UNAMBIGUOUS US signals only. Deliberately NOT added:
    # "Senate", "Supreme Court", "federal", "governor" — every one of these names an
    # institution that exists in many countries, and a lone ambiguous word must never
    # make a story American. State names below exclude Georgia (a country) and
    # Washington (already matched above, and ambiguous with the UK place name usage is
    # negligible for the state list chosen).
    ("capitol-hill", re.compile(r"\bcapitol hill\b", re.I)),
    ("pentagon", re.compile(r"\bthe pentagon\b", re.I)),
    ("fbi", re.compile(r"\bfbi\b", re.I)),
    ("us-state", re.compile(
        r"\b(california|texas|florida|new york|pennsylvania|illinois|ohio|"
        r"michigan|arizona|colorado|massachusetts|virginia|oregon|nevada|"
        r"alabama|louisiana|kentucky|oklahoma|minnesota|wisconsin)\b", re.I)),
)
_WORLD = (
    ("united-nations", re.compile(r"\bunited nations\b|\bu\.n\.\b", re.I)),
    ("multinational", re.compile(r"\bmultinational\b|\binternational coalition\b", re.I)),
    ("g7", re.compile(r"\bg7\b|\bgroup of seven\b", re.I)),
    ("global-treaty", re.compile(r"\bglobal (?:treaty|agreement|summit)\b", re.I)),
)
_PATTERNS = {"japan": _JAPAN, "united_states": _US, "world": _WORLD}


def _structured_regions(candidate: dict) -> set[str]:
    raw = candidate.get("structuredRegions") or candidate.get("structured_regions") or []
    if isinstance(raw, str):
        raw = [raw]
    try:
        return set(normalize_regions(raw))
    except ValueError:
        return set()


def classify_region(candidate: dict, region: str) -> dict:
    canonical = normalize_regions([region])[0]
    if canonical in _structured_regions(candidate):
        return {"region": canonical, "strength": "primary",
                "evidence": [f"{canonical}:structured"]}

    evidence = []
    by_field = {}
    for field in ("title", "summary", "metadata"):
        value = candidate.get(field, "")
        if isinstance(value, dict):
            value = " ".join(str(v) for v in value.values())
        text = str(value or "")
        hits = [f"{label}:{field}" for label, rx in _PATTERNS[canonical] if rx.search(text)]
        if hits:
            by_field[field] = hits
            evidence.extend(hits)

    title_hits = by_field.get("title", [])
    summary_hits = by_field.get("summary", [])
    metadata_hits = by_field.get("metadata", [])

    # A publisher's home region is never evidence. A lone "Japanese" attached to a
    # company is descriptive company metadata, not proof that Japan is the story's locus.
    title_blob = str(candidate.get("title", ""))
    japanese_company_only = (
        canonical == "japan"
        and title_hits == ["japanese:title"]
        and _COMPANIES.search(title_blob)
        and not summary_hits
        and not metadata_hits
    )
    if japanese_company_only:
        return {"region": canonical, "strength": "incidental", "evidence": evidence}

    # Headline evidence makes the geography central. In body/summary text, require two
    # independent signals; one passing mention is explicitly incidental.
    primary = bool(title_hits) or len(summary_hits) >= 2 or bool(metadata_hits)
    return {
        "region": canonical,
        "strength": "primary" if primary else ("incidental" if evidence else "none"),
        "evidence": evidence,
    }


def classify_regions(candidate: dict) -> list[dict]:
    return [classify_region(candidate, region) for region in ("japan", "united_states", "world")]
