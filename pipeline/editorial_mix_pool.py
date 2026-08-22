#!/usr/bin/env python3
"""Offline Editorial Mix Pool builder. (Phase 3D-3C.2)

Turns a frozen raw Mix Pool into a fully validated `editorial-mix-pool` artifact, so that at
request time the API only has to load, validate, select and serialize — never fetch an
article, run the writer, or look up an image.

REUSE, NOT REIMPLEMENTATION. Every editorial decision comes from the existing production
code:
    writer.get_source_text   article fetch + cache, unchanged paths and limits
    writer.draft_one         summary, keyTakeaways, whyItMatters, readTime
    build.assign_images      image selection, rotation and cooldown avoidance
    build.read_time_int      the exact integer-minute normalisation iOS expects
There is no second prompt, no second summariser and no second image allocator here.

WHY writer.py IS CHEAP. It makes no model call — every field is composed deterministically
from cleaned article text. The incremental cost of enriching 20 candidates instead of 5 is
15 extra ARTICLE FETCHES (fewer when the cache is warm), not 15 extra LLM calls.

COUNTS. Target 20 candidates, minimum 15 survivors. A per-candidate fetch or writer failure
removes only that candidate — one bad source must not cost the whole day — but the artifact
is all-or-nothing below 15, because a pool that thin cannot reliably yield five stories
across arbitrary user mixes.

IMAGES. Assigned once across the WHOLE surviving pool, so every candidate holds a distinct
image and therefore any selected five inherit uniqueness. The existing 90-edition cooldown
is passed straight through. Custom Mix deliberately gives up the standard edition's
"lead gets the freshest image" optimisation: the lead is not known until request-time
selection, so no lead index is supplied.

This module does not publish. Storage, workflow integration and `/api/edition` connection
are all still out of scope.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))

try:  # package-relative first, matching the rest of the pipeline
    from . import build as build_module
    from . import writer as writer_module
    from .editorial_mix_pool_schema import (
        MINIMUM_PUBLISHABLE_POOL_SIZE,
        TARGET_POOL_SIZE,
        freeze_editorial_artifact,
        validate_editorial_mix_pool,
    )
    from .mix_pool_schema import pool_identity, validate_artifact
except ImportError:  # direct script/module use
    import build as build_module  # type: ignore[no-redef]
    import writer as writer_module  # type: ignore[no-redef]
    from editorial_mix_pool_schema import (  # type: ignore[no-redef]
        MINIMUM_PUBLISHABLE_POOL_SIZE,
        TARGET_POOL_SIZE,
        freeze_editorial_artifact,
        validate_editorial_mix_pool,
    )
    from mix_pool_schema import pool_identity, validate_artifact  # type: ignore[no-redef]

#: Tolerated candidate-level failures for a full 20-candidate input (20 - 15).
MAX_CANDIDATE_FAILURES = TARGET_POOL_SIZE - MINIMUM_PUBLISHABLE_POOL_SIZE

#: Stable, safe failure categories. No article text, URL, prompt or credential ever appears.
REASON_INVALID_RAW_POOL = "editorial_pool_invalid_raw_pool"
REASON_INSUFFICIENT_INPUT = "editorial_pool_insufficient_input"
REASON_ARTICLE_FETCH_FAILED = "editorial_pool_article_fetch_failed"
REASON_WRITER_FAILED = "editorial_pool_writer_failed"
REASON_INVALID_WRITER_OUTPUT = "editorial_pool_invalid_writer_output"
REASON_INSUFFICIENT_STORY_SURVIVORS = "editorial_pool_insufficient_story_survivors"
REASON_IMAGE_ASSIGNMENT_FAILED = "editorial_pool_image_assignment_failed"
REASON_INVALID_IMAGE = "editorial_pool_invalid_image"
REASON_DUPLICATE_IMAGE = "editorial_pool_duplicate_image"
REASON_INSUFFICIENT_FINAL_SURVIVORS = "editorial_pool_insufficient_final_survivors"
REASON_IDENTITY_MISMATCH = "editorial_pool_identity_mismatch"
REASON_VALIDATION_FAILED = "editorial_pool_validation_failed"

#: Writer flags that make a draft unusable for publication (mirrors build.BLOCKING_FLAGS).
BLOCKING_FLAGS = getattr(
    build_module, "BLOCKING_FLAGS",
    {"needs_review", "source_unavailable", "thin_source", "whyItMatters_needs_human"},
)


class EditorialPoolError(Exception):
    """An artifact-level failure. `reason` is a stable category; the message carries no payload."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason
        self.detail = detail


def _candidate_failure(candidate_id: str, reason: str, stage: str) -> dict[str, str]:
    """A SAFE per-candidate failure record: id, category and stage only."""
    return {"id": str(candidate_id), "reason": reason, "stage": stage}


def _eligible(candidate: dict[str, Any]) -> bool:
    if candidate.get("eligible") is False:
        return False
    quality = candidate.get("quality")
    if isinstance(quality, dict) and quality.get("eligible") is False:
        return False
    return True


def validate_raw_pool(raw_artifact: Any, *, expected_date: str | None = None) -> list[dict[str, Any]]:
    """
    Accept only an already frozen, schema-valid raw Mix Pool. Raises before any external
    work so a bad input never costs a single article fetch.
    """
    if not isinstance(raw_artifact, dict):
        raise EditorialPoolError(REASON_INVALID_RAW_POOL, "artifact must be an object")

    result = validate_artifact(raw_artifact)
    if not result["valid"]:
        raise EditorialPoolError(REASON_INVALID_RAW_POOL, "; ".join(result["errors"][:3]))

    candidates = raw_artifact.get("candidates") or []
    if pool_identity(candidates) != raw_artifact.get("poolIdentity"):
        raise EditorialPoolError(REASON_IDENTITY_MISMATCH, "poolIdentity does not match candidates")
    if expected_date is not None and raw_artifact.get("date") != expected_date:
        raise EditorialPoolError(REASON_INVALID_RAW_POOL, "date does not match the requested date")

    eligible = [c for c in candidates if _eligible(c)]
    if len(eligible) < MINIMUM_PUBLISHABLE_POOL_SIZE:
        raise EditorialPoolError(
            REASON_INSUFFICIENT_INPUT,
            f"{len(eligible)} eligible candidates, need {MINIMUM_PUBLISHABLE_POOL_SIZE}",
        )
    return eligible


#: Regional coverage floors for the enrichment pool (as available in the raw pool).
#: 6+4+4 = 14 of the 20 slots guarantee that a US-first, japan-only or world-heavy mix
#: can reach five stories; the remaining slots restore topic coverage and score order.
_ENRICHMENT_REGION_FLOORS = (("united_states", 6), ("japan", 4), ("world", 4))
_ENRICHMENT_TOPICS = ("ai", "business", "climate", "culture", "health", "science", "tech")


def _is_primary(candidate: dict[str, Any], region: str) -> bool:
    for row in candidate.get("regionMemberships") or []:
        if (row.get("region") or row.get("id")) == region and row.get("strength") == "primary":
            return True
    return False


def select_for_enrichment(eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    COVERAGE-AWARE choice of the `TARGET_POOL_SIZE` candidates to enrich. (2026-08-18)

    The old rule — "first 20 in id order" — was blind to what the pool contained, so a
    UK-heavy Scout day could produce an enrichment pool with two US stories and no
    culture story, making "always five" impossible for whole classes of user settings.
    Deterministic, in three passes over a score-then-id ordering:

      1. REGIONAL FLOORS: the top-scoring primaries per region, US first (6/4/4).
      2. TOPIC COVERAGE: one candidate per canonical topic that is still missing.
      3. SCORE FILL: remaining slots by score, id as the tie-break.

    No new ranking function is introduced — `baseScore` is the pool's own score and the
    id tie-break keeps the result independent of input order.
    """
    by_score = sorted(
        eligible,
        key=lambda c: (-float(c.get("baseScore", 0)), str(c.get("id", ""))),
    )
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()

    def take(candidate: dict[str, Any]) -> bool:
        if len(chosen) >= TARGET_POOL_SIZE:
            return False
        identifier = str(candidate.get("id", ""))
        if identifier in chosen_ids:
            return False
        chosen.append(candidate)
        chosen_ids.add(identifier)
        return True

    for region, floor in _ENRICHMENT_REGION_FLOORS:
        have = sum(1 for c in chosen if _is_primary(c, region))
        for candidate in by_score:
            if have >= floor:
                break
            if str(candidate.get("id", "")) in chosen_ids:
                continue
            if _is_primary(candidate, region) and take(candidate):
                have += 1

    for topic in _ENRICHMENT_TOPICS:
        if any(topic in (c.get("topics") or []) for c in chosen):
            continue
        for candidate in by_score:
            if str(candidate.get("id", "")) in chosen_ids:
                continue
            if topic in (candidate.get("topics") or []):
                take(candidate)
                break

    for candidate in by_score:
        if len(chosen) >= TARGET_POOL_SIZE:
            break
        take(candidate)
    return chosen


def enrich_story(
    candidate: dict[str, Any],
    *,
    articles_dir: str,
    allow_fetch: bool,
    unavailable: set[str] | None = None,
    get_source_text: Callable[..., tuple[str, str]] | None = None,
    draft_one: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Run the REAL writer over one candidate and return its story-level editorial block.

    Raises `EditorialPoolError` with a candidate-level reason on failure. The candidate is
    never mutated: `draft_one` receives a copy.
    """
    fetch = get_source_text or writer_module.get_source_text
    draft = draft_one or writer_module.draft_one

    # `writer` expects the scout-style shape; build it from the frozen selector candidate.
    item = {
        "id": candidate["id"],
        "title": candidate.get("headline", ""),
        "source": candidate.get("source", ""),
        "url": candidate.get("url", ""),
        "category": candidate.get("category", "OTHER"),
        "snippet": candidate.get("summary", ""),
    }

    try:
        source_text, used = fetch(item, articles_dir, allow_fetch, unavailable or set())
    except Exception as error:  # noqa: BLE001 - the provider's message must never escape
        raise EditorialPoolError(REASON_ARTICLE_FETCH_FAILED, type(error).__name__) from None

    if used == "none" or not source_text:
        raise EditorialPoolError(REASON_ARTICLE_FETCH_FAILED, "no usable source text")

    try:
        result = draft(copy.deepcopy(item), source_text, used)
    except Exception as error:  # noqa: BLE001
        raise EditorialPoolError(REASON_WRITER_FAILED, type(error).__name__) from None

    if not isinstance(result, dict) or not isinstance(result.get("draft"), dict):
        raise EditorialPoolError(REASON_INVALID_WRITER_OUTPUT, "writer returned no draft")

    blocking = sorted(set(result.get("flags") or []) & set(BLOCKING_FLAGS))
    if blocking:
        raise EditorialPoolError(REASON_INVALID_WRITER_OUTPUT, ",".join(blocking))

    body = result["draft"]
    headline = str(body.get("headline") or "").strip()
    summary = str(body.get("summary") or "").strip()
    why = str(body.get("whyItMatters") or "").strip()
    takeaways = [str(t).strip() for t in (body.get("keyTakeaways") or []) if str(t).strip()]
    read_time = build_module.read_time_int(body.get("readTime"))

    if not headline or not summary or not why or not takeaways or read_time < 1:
        raise EditorialPoolError(REASON_INVALID_WRITER_OUTPUT, "missing required editorial field")

    return {
        "category": candidate.get("category", "OTHER"),
        "source": candidate.get("source", ""),
        "headline": headline,
        "summary": summary,
        "keyTakeaways": takeaways,
        "whyItMatters": why,
        "originalURL": candidate.get("url", ""),
        "readTime": read_time,
        "imageURL": "",   # assigned across the whole pool below
        "placeTime": "",
        "audioURL": "",   # v1 compatibility: the live feed carries ""
    }


def assign_pool_images(
    enriched: list[dict[str, Any]],
    *,
    edition_date: str,
    images_config: dict[str, Any],
    avoid: set[str],
    seen_ever: set[str] | None = None,
    assign: Callable[..., list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """
    Assign one image per candidate across the WHOLE pool using the production allocator.

    No `lead_index` is supplied: Custom Mix cannot know the lead offline, so the standard
    edition's lead-first freshness優先 is deliberately not reproduced. Candidates that
    receive no usable image are dropped, preserving the relative order of the rest.
    """
    allocator = assign or build_module.assign_images
    if not enriched:
        return [], []

    items = [
        {
            "number": index + 1,  # positional only; never stored on a candidate
            "headline": row["editorial"]["headline"],
            "summary": row["editorial"]["summary"],
            "category": row["editorial"]["category"],
        }
        for index, row in enumerate(enriched)
    ]

    try:
        picks = allocator(
            items,
            images_config.get("category_pools", {}),
            images_config.get("aliases", {}),
            images_config.get("default_pool", []),
            images_config.get("topic_pools", {}),
            images_config.get("topic_matchers", {}),
            dt.date.fromisoformat(edition_date).timetuple().tm_yday,
            img_pool=images_config.get("pool", []),
            img_cats=images_config.get("cats", {}),
            img_default=images_config.get("default", {"imageURL": "", "placeTime": ""}),
            avoid=set(avoid),
            lead_index=None,
            seen_ever=set(seen_ever or ()),
            log=lambda *args, **kwargs: None,   # never log story content
        )
    except Exception as error:  # noqa: BLE001
        raise EditorialPoolError(REASON_IMAGE_ASSIGNMENT_FAILED, type(error).__name__) from None

    if not isinstance(picks, list) or len(picks) != len(enriched):
        raise EditorialPoolError(REASON_IMAGE_ASSIGNMENT_FAILED, "allocator returned a bad shape")

    survivors: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    used_images: set[str] = set()

    for row, pick in zip(enriched, picks):
        candidate_id = str(row["selector"]["id"])
        url = str((pick or {}).get("imageURL") or "")
        if not url.startswith("https://"):
            failures.append(_candidate_failure(candidate_id, REASON_INVALID_IMAGE, "image"))
            continue
        if url in used_images:
            failures.append(_candidate_failure(candidate_id, REASON_DUPLICATE_IMAGE, "image"))
            continue
        used_images.add(url)

        enriched_row = copy.deepcopy(row)
        enriched_row["editorial"]["imageURL"] = url
        enriched_row["editorial"]["placeTime"] = str((pick or {}).get("placeTime") or "")
        survivors.append(enriched_row)

    return survivors, failures


def build_editorial_mix_pool(
    raw_artifact: dict[str, Any],
    *,
    generated_at: str,
    articles_dir: str,
    images_config: dict[str, Any],
    avoid_images: set[str] | None = None,
    seen_ever: set[str] | None = None,
    allow_fetch: bool = True,
    source: str = "daily-pipeline",
    get_source_text: Callable[..., tuple[str, str]] | None = None,
    draft_one: Callable[..., dict[str, Any]] | None = None,
    assign: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """
    The whole offline path. Returns a validated artifact, or raises `EditorialPoolError`.

    Never mutates `raw_artifact`, never publishes, and never returns a partial artifact.
    """
    eligible = validate_raw_pool(raw_artifact)
    chosen = select_for_enrichment(eligible)

    enriched: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for candidate in chosen:
        selector = copy.deepcopy(candidate)
        try:
            editorial = enrich_story(
                selector,
                articles_dir=articles_dir,
                allow_fetch=allow_fetch,
                get_source_text=get_source_text,
                draft_one=draft_one,
            )
        except EditorialPoolError as error:
            failures.append(
                _candidate_failure(selector.get("id", ""), error.reason, "story")
            )
            continue
        enriched.append({"selector": selector, "editorial": editorial})

    if len(enriched) < MINIMUM_PUBLISHABLE_POOL_SIZE:
        raise EditorialPoolError(
            REASON_INSUFFICIENT_STORY_SURVIVORS,
            f"{len(enriched)} survived story enrichment, need {MINIMUM_PUBLISHABLE_POOL_SIZE}",
        )

    survivors, image_failures = assign_pool_images(
        enriched,
        edition_date=str(raw_artifact["date"]),
        images_config=images_config,
        avoid=set(avoid_images or ()),
        seen_ever=seen_ever,
        assign=assign,
    )
    failures.extend(image_failures)

    if len(survivors) < MINIMUM_PUBLISHABLE_POOL_SIZE:
        raise EditorialPoolError(
            REASON_INSUFFICIENT_FINAL_SURVIVORS,
            f"{len(survivors)} survived image assignment, need {MINIMUM_PUBLISHABLE_POOL_SIZE}",
        )

    artifact = freeze_editorial_artifact(
        date=str(raw_artifact["date"]),
        generated_at=generated_at,
        candidates=survivors,
        source=source,
    )

    if artifact["selectorPoolIdentity"] != pool_identity(
        [row["selector"] for row in sorted(survivors, key=lambda r: str(r["selector"]["id"]))]
    ):
        raise EditorialPoolError(REASON_IDENTITY_MISMATCH, "selector identity drifted")

    result = validate_editorial_mix_pool(artifact)
    if not result["valid"]:
        raise EditorialPoolError(REASON_VALIDATION_FAILED, "; ".join(result["errors"][:3]))

    artifact["_failures"] = failures   # caller-only diagnostics; stripped below
    safe_failures = artifact.pop("_failures")
    return {"artifact": artifact, "failures": safe_failures, "candidateCount": len(survivors)}
