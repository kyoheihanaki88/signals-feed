#!/usr/bin/env python3
"""
Shared editorial helpers — Signals Feed Editorial Quality v2.1.

Topic fingerprinting + duplicate-topic detection, kept in ONE place so the Ranker (selection) and
build.py (final composition gate) judge "same story" identically. Deterministic, stdlib-only — NO
LLM. The point: two signals about the same event (e.g. a U.S.–Iran peace deal) must not both make
the daily five, even when their URLs and headlines differ.
"""
import re

# Canonical tokens collapse synonyms so near-identical topics share a fingerprint.
#   ENTITY = a specific actor (country / company / government).
#   THEME  = the kind of story.
# A story's fingerprint is the set of canon tokens its title (+summary) triggers.
_SYNONYMS = [
    # ── entities ───────────────────────────────────────────────────────────────────────────────
    (re.compile(r"\b(u\.?\s?s\.?\s?a?\.?|united states|america|american|washington|white house|"
                r"trump administration|the administration|president trump|\btrump\b|\bbiden\b|"
                r"pentagon|congress)\b", re.I), "usa"),
    (re.compile(r"\b(iran|iranian|tehran)\b", re.I), "iran"),
    (re.compile(r"\b(israel|israeli|tel aviv|netanyahu)\b", re.I), "israel"),
    (re.compile(r"\b(pakistan|pakistani|islamabad)\b", re.I), "pakistan"),
    (re.compile(r"\b(china|chinese|beijing|xi jinping)\b", re.I), "china"),
    (re.compile(r"\b(russia|russian|moscow|kremlin|putin)\b", re.I), "russia"),
    (re.compile(r"\b(ukraine|ukrainian|kyiv|kiev|zelensky)\b", re.I), "ukraine"),
    (re.compile(r"\b(gaza|hamas|palestin\w+)\b", re.I), "gaza"),
    (re.compile(r"\b(european union|\beu\b|brussels)\b", re.I), "eu"),
    (re.compile(r"\b(amazon|\baws\b)\b", re.I), "amazon"),
    (re.compile(r"\b(anthropic|claude)\b", re.I), "anthropic"),
    (re.compile(r"\b(openai|chatgpt|sam altman)\b", re.I), "openai"),
    (re.compile(r"\b(google|alphabet|deepmind|gemini)\b", re.I), "google"),
    (re.compile(r"\b(microsoft|azure)\b", re.I), "microsoft"),
    (re.compile(r"\b(meta|facebook|instagram)\b", re.I), "meta"),
    (re.compile(r"\b(apple|iphone)\b", re.I), "apple"),
    (re.compile(r"\b(nvidia)\b", re.I), "nvidia"),
    # ── themes ─────────────────────────────────────────────────────────────────────────────────
    (re.compile(r"\b(peace|ceasefire|truce|talks?|negotiat\w+|\bdeal\b|agreement|accord|diplomacy|"
                r"summit)\b", re.I), "peace_talks"),
    (re.compile(r"\b(tariff\w*|trade war|trade deal|import\w*|export\w*|supply chain)\b", re.I), "trade"),
    (re.compile(r"\b(antitrust|monopoly|competition probe)\b", re.I), "antitrust"),
    (re.compile(r"\b(\bai\b|a\.i\.|artificial intelligence)\b", re.I), "ai"),
    (re.compile(r"\b(climate|emissions|warming|carbon|wildfire|flood\w*|drought)\b", re.I), "climate"),
    (re.compile(r"\b(election|\bvote\b|ballot|primary|campaign)\b", re.I), "election"),
    (re.compile(r"\b(\bwar\b|military|missile|airstrike|troops|nuclear)\b", re.I), "military"),
    (re.compile(r"\b(inflation|interest rate|recession|\bgdp\b|markets?|stocks?|\bbond\b)\b", re.I), "markets"),
]

ENTITY_CANON = {"usa", "iran", "israel", "pakistan", "china", "russia", "ukraine", "gaza", "eu",
                "amazon", "anthropic", "openai", "google", "microsoft", "meta", "apple", "nvidia"}
THEME_CANON = {"peace_talks", "trade", "antitrust", "ai", "climate", "election", "military", "markets"}


def topic_fingerprint(*texts):
    """Canonical topic tokens for one story (pass title, optionally + summary). A frozenset so the
    caller can do set algebra; empty when nothing canonical is recognized."""
    blob = " ".join(t or "" for t in texts)
    return frozenset(canon for rx, canon in _SYNONYMS if rx.search(blob))


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def topics_overlap(a, b):
    """True when two fingerprints describe the same / near-identical story (duplicate coverage).
    Same two actors → same story; one shared actor + same theme + high overlap → same story;
    otherwise a high Jaccard. Tuned so US/Iran peace-deal variants collide but distinct stories
    (e.g. an Amazon/Anthropic policy story vs an Iran story) do not."""
    if not a or not b:
        return False
    shared = a & b
    if len(shared & ENTITY_CANON) >= 2:
        return True
    if (shared & ENTITY_CANON) and (shared & THEME_CANON) and _jaccard(a, b) >= 0.5:
        return True
    return _jaccard(a, b) >= 0.6


def first_duplicate_pair(fingerprints):
    """Given an ordered list of (label, fingerprint), return the first (i, j) pair that overlaps,
    or None. Used by the final composition gate to report which two signals collide."""
    for i in range(len(fingerprints)):
        for j in range(i + 1, len(fingerprints)):
            if topics_overlap(fingerprints[i][1], fingerprints[j][1]):
                return fingerprints[i][0], fingerprints[j][0]
    return None
