#!/usr/bin/env python3
"""Build and publish the daily Editorial Mix Pool. (Phase 3D-3D)

The daily workflow's Custom Mix entry point, and the ONLY place the offline builder meets
the Upstash transport. Two explicit subcommands, no default action:

    build    scout candidates  ->  raw Mix Pool  ->  Editorial Mix Pool  ->  a JSON file
    publish  a built file      ->  revalidated   ->  one atomic Upstash SET

NOTHING HAPPENS ON IMPORT, and nothing happens without a subcommand. `publish` requires
credentials in the environment; without them it exits with a distinct, visible code rather
than pretending the pool was published.

DESTINATION SAFETY. `build` refuses to write anywhere inside the repository, mirroring
`mix_pool_cli.py`. The generated pool is runner-local and must never be committed: it is
derived data with a 9-day life, and the repository is a public artifact store.

STANDARD EDITION ISOLATION. This CLI reads the scout candidate file and the images config;
it writes nothing the standard edition reads, touches no `editions/` file and never
modifies `latest.json`. A failure here cannot roll back a built standard edition.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import build as build_module
import mix_pool
import mix_pool_schema
from editorial_mix_pool import EditorialPoolError, build_editorial_mix_pool
from editorial_mix_pool_publisher import (
    EditorialPublishError,
    publish_editorial_mix_pool,
)
from editorial_mix_pool_schema import (
    MINIMUM_PUBLISHABLE_POOL_SIZE,
    validate_editorial_mix_pool,
)
from upstash_mix_pool_transport import UpstashTransportError, create_store

#: Distinct exit codes so the workflow can tell "not configured yet" from "it broke".
EXIT_OK = 0
EXIT_BUILD_FAILED = 3
#: Distinct from EXIT_BUILD_FAILED: a raw-pool problem is a SCOUT-SHAPE problem, and must
#: never be mistaken for an enrichment problem.
EXIT_RAW_POOL_FAILED = 6
EXIT_NOT_CONFIGURED = 4
EXIT_PUBLISH_FAILED = 5
EXIT_USAGE = 2

IMAGE_REUSE_WINDOW = 90


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_destination(path: Path) -> Path:
    """Refuse any repository destination. Generated pool data is never committed."""
    resolved = path.expanduser().resolve()
    if _inside(resolved, REPO_ROOT.resolve()):
        raise ValueError("refusing a repository destination for generated pool data")
    return resolved


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mixpool_canonical(row: dict[str, Any]) -> str:
    """The canonical URL `mix_pool` will derive. Imported, never re-implemented here."""
    return mix_pool._canonical_url(row.get("canonical_url") or row.get("url"))


def prepare_scout_source(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """
    Adapt a scout/ranker candidate document to what `build_mix_pool` can actually accept.

    WHY THIS EXISTS — two shape mismatches that only appear on live feeds:

      1. EMPTY RSS DESCRIPTION. `ranker.likely_complete` returns True for any reliable,
         non-paywalled source regardless of snippet length, so an entry with no
         <description> survives exclusion, normalises to `summary: ""`, and then
         `validate_mix_pool` rejects the WHOLE pool with "empty required copy". ONE such
         entry anywhere kills the entire build. The standard edition is immune: it fetches
         the article body and composes its own summary, and never calls `validate_mix_pool`.

      2. CANONICALISATION DISAGREEMENT. `scout.canonical_url` KEEPS meaningful query
         parameters; `mix_pool._canonical_url` DROPS the query. Because the candidate id is
         hashed over the query-bearing URL, `resolvable()` then compares it against
         sha1(query-stripped URL) and every affected candidate is silently dropped as
         `unresolvable_candidate_id`.

    Both are filtered HERE, at the adapter layer. `mix_pool.py`, `validate_mix_pool`, the
    numeric rules, the schema version, the canonical hashing and the four pinned identity
    baselines are untouched, and the standard edition path is not involved at all.

    No ranking logic is duplicated: eligibility, scoring and exclusion remain entirely with
    `ranker`/`mix_pool`. This only removes rows the raw contract cannot represent.

    Deterministic: input order is preserved and the FIRST occurrence of a duplicate
    canonical wins. The caller's document is never mutated — a new dict is returned, and the
    candidate rows are passed through by reference without modification.
    """
    rows = source.get("candidates")
    if not isinstance(rows, list):
        raise EditorialPoolError("raw_pool_source_invalid", "candidates must be a list")

    kept: list[dict[str, Any]] = []
    seen_canonical: set[str] = set()
    dropped = {"empty_copy": 0, "unusable_url": 0, "duplicate_canonical": 0}

    for row in rows:
        if not isinstance(row, dict):
            dropped["empty_copy"] += 1
            continue
        # `validate_mix_pool` requires non-empty headline, summary and source.
        if not all(str(row.get(key) or "").strip() for key in ("title", "snippet", "source")):
            dropped["empty_copy"] += 1
            continue
        canonical = _mixpool_canonical(row)
        if not canonical:
            dropped["unusable_url"] += 1
            continue
        if canonical in seen_canonical:
            dropped["duplicate_canonical"] += 1
            continue
        seen_canonical.add(canonical)
        kept.append(row)

    return {**source, "candidates": kept}, dropped


#: Ordered: first match wins. Every rule maps to a STABLE category name.
_MIX_POOL_ERROR_RULES: tuple[tuple[str, str], ...] = (
    ("empty required copy", "empty_required_copy"),
    ("duplicate candidate id", "duplicate_candidate_id"),
    ("duplicate canonical URL", "duplicate_canonical_url"),
    ("poolIdentity", "identity_mismatch"),
    ("category", "invalid_taxonomy"),
    ("topics", "invalid_taxonomy"),
    ("region", "invalid_taxonomy"),
    ("baseScore", "invalid_numeric_field"),
    ("sourceRisk", "invalid_numeric_field"),
    ("clusterS", "invalid_numeric_field"),
    ("candidateCount", "schema_invalid"),
    ("schemaVersion", "schema_invalid"),
    ("invalid URL", "schema_invalid"),
    ("missing", "schema_invalid"),
    ("date must be", "schema_invalid"),
    ("generatedAt", "schema_invalid"),
    ("publishedAt", "schema_invalid"),
    ("candidates must be a list", "schema_invalid"),
)


def safe_mix_pool_error(error: Exception) -> dict[str, int]:
    """
    Reduce a `MixPoolError` to stable category counts.

    Its message names the failing field paths, which would be useful — but it also embeds
    candidate URLs and ids, which must never be logged. So the prose is discarded entirely
    and only the SHAPE of the failure survives: category -> count. Nothing derived from a
    headline, summary, URL, body or credential can escape through this function.
    """
    categories: dict[str, int] = {}
    for part in str(error).split("; "):
        label = "unknown_mix_pool_error"
        for needle, category in _MIX_POOL_ERROR_RULES:
            if needle in part:
                label = category
                break
        categories[label] = categories.get(label, 0) + 1
    return categories


def _load_images(path: Path) -> dict[str, Any]:
    """Load images.yaml into the shape `assign_pool_images` expects."""
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "category_pools": raw.get("category_pools", {}),
        "aliases": raw.get("aliases", {}),
        "default_pool": raw.get("default_pool", []),
        "topic_pools": raw.get("topic_pools", {}),
        "topic_matchers": build_module.build_topic_matchers(raw.get("topic_keywords", {})),
        "pool": raw.get("pool", []),
        "cats": raw.get("categories", {}),
        "default": raw.get("default", {"imageURL": "", "placeTime": ""}),
    }


def command_build_raw(args: argparse.Namespace) -> int:
    """
    STAGE 1 — scout/ranker candidates -> frozen, validated raw Mix Pool artifact.

    Split out so the raw pool is an inspectable artifact and a scout-shape failure is
    attributable to this stage, with its own exit code, instead of surfacing as a bare
    exception class name from inside a combined step.
    """
    destination = _check_destination(Path(args.output))
    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    input_count = len(source.get("candidates") or [])
    prepared, dropped = prepare_scout_source(source)

    try:
        raw_pool = mix_pool.build_mix_pool(prepared, args.date, args.generated_at)
    except mix_pool.MixPoolError as error:
        print(json.dumps({"status": "failed", "stage": "raw_pool",
                          "reason": "mix_pool_invalid",
                          "categories": safe_mix_pool_error(error),
                          "inputCount": input_count,
                          "droppedBeforeBuild": dropped},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return EXIT_RAW_POOL_FAILED

    frozen = mix_pool_schema.freeze_artifact(
        raw_pool, source_input=prepared, source=args.source
    )
    validation = mix_pool_schema.validate_artifact(frozen)
    if not validation["valid"]:
        print(json.dumps({"status": "failed", "stage": "raw_pool",
                          "reason": "raw_artifact_invalid",
                          "errorCount": len(validation["errors"])},
                         sort_keys=True), file=sys.stderr)
        return EXIT_RAW_POOL_FAILED

    body = mix_pool_schema.serialize(frozen)
    _atomic_write(destination, body)
    print(json.dumps({
        "status": "built",
        "stage": "raw_pool",
        "date": frozen["date"],
        "inputCount": input_count,
        "droppedBeforeBuild": dropped,
        "candidateCount": frozen["candidateCount"],
        "byteLength": len(body),
        "poolIdentityPrefix": str(frozen["poolIdentity"])[:12],
    }, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def command_validate_raw(args: argparse.Namespace) -> int:
    """
    STAGE 2 — revalidate the frozen raw artifact and re-derive its identity.

    Pure: reads one file, calls no network, mutates nothing.
    """
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    validation = mix_pool_schema.validate_artifact(artifact)
    identity_ok = (
        mix_pool_schema.pool_identity(artifact.get("candidates") or [])
        == artifact.get("poolIdentity")
    )
    if not validation["valid"] or not identity_ok:
        print(json.dumps({"status": "invalid", "stage": "raw_pool",
                          "errorCount": len(validation["errors"]),
                          "identityMatches": identity_ok},
                         sort_keys=True), file=sys.stderr)
        return EXIT_RAW_POOL_FAILED

    print(json.dumps({
        "status": "valid",
        "stage": "raw_pool",
        "date": artifact["date"],
        "candidateCount": artifact["candidateCount"],
        "identityMatches": True,
        "poolIdentityPrefix": str(artifact["poolIdentity"])[:12],
    }, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def command_build(args: argparse.Namespace) -> int:
    """
    STAGE 3 — frozen raw Mix Pool artifact -> enriched, validated Editorial Mix Pool.

    Takes `--raw-input` ONLY. Scout/ranker candidates are no longer accepted here: the raw
    stage has its own subcommand precisely so a scout-shape problem can never be mistaken
    for an enrichment problem. The raw artifact is revalidated before any enrichment work
    begins, so a corrupted intermediate costs zero article fetches.
    """
    destination = _check_destination(Path(args.output))
    frozen_raw = json.loads(Path(args.raw_input).read_text(encoding="utf-8"))

    raw_validation = mix_pool_schema.validate_artifact(frozen_raw)
    if not raw_validation["valid"]:
        print(json.dumps({"status": "failed", "stage": "editorial_pool",
                          "reason": "raw_input_invalid",
                          "errorCount": len(raw_validation["errors"])},
                         sort_keys=True), file=sys.stderr)
        return EXIT_RAW_POOL_FAILED

    editions_dir = str(REPO_ROOT / "editions")
    avoid = build_module.recent_image_urls(editions_dir, args.date, window=IMAGE_REUSE_WINDOW)
    seen_ever = build_module.recent_image_urls(editions_dir, args.date, window=10**9)

    result = build_editorial_mix_pool(
        frozen_raw,
        generated_at=args.generated_at,
        articles_dir=args.articles,
        images_config=_load_images(Path(args.images)),
        avoid_images=avoid,
        seen_ever=seen_ever,
        allow_fetch=not args.no_fetch,
        source=args.source,
    )

    artifact = result["artifact"]
    validation = validate_editorial_mix_pool(artifact)
    if not validation["valid"]:
        print(json.dumps({"status": "failed", "stage": "editorial_pool",
                          "reason": "editorial_artifact_invalid",
                          "errorCount": len(validation["errors"])},
                         sort_keys=True), file=sys.stderr)
        return EXIT_BUILD_FAILED

    body = mix_pool_schema.serialize(artifact)
    _atomic_write(destination, body)
    print(json.dumps({
        "status": "built",
        "stage": "editorial_pool",
        "date": artifact["date"],
        "candidateCount": artifact["candidateCount"],
        "minimumRequired": MINIMUM_PUBLISHABLE_POOL_SIZE,
        "byteLength": len(body),
        "failureReasons": sorted({row["reason"] for row in result.get("failures", [])}),
        "selectorPoolIdentityPrefix": str(artifact["selectorPoolIdentity"])[:12],
        "editorialPoolIdentityPrefix": str(artifact["editorialPoolIdentity"])[:12],
    }, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def command_publish(args: argparse.Namespace) -> int:
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))

    # Revalidate at the publication boundary. The file may have been built minutes ago by
    # another step; publication must never trust that, only the bytes in front of it.
    validation = validate_editorial_mix_pool(artifact)
    if not validation["valid"]:
        print(
            json.dumps({"status": "rejected", "reason": "editorial_pool_invalid",
                        "errorCount": len(validation["errors"])}),
            file=sys.stderr,
        )
        return EXIT_PUBLISH_FAILED

    try:
        store = create_store(dict(os.environ), timeout_seconds=args.timeout)
    except UpstashTransportError as error:
        print(json.dumps({"status": "not_configured", "reason": error.reason}), file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    try:
        result = publish_editorial_mix_pool(artifact, store=store, date=args.date)
    except EditorialPublishError as error:
        print(json.dumps({"status": "rejected", "reason": error.reason}), file=sys.stderr)
        return EXIT_PUBLISH_FAILED
    except UpstashTransportError as error:
        print(json.dumps({"status": "failed", "reason": error.reason}), file=sys.stderr)
        return EXIT_PUBLISH_FAILED

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Build and publish the daily Editorial Mix Pool. No default action."
    )
    sub = root.add_subparsers(dest="command", required=True)

    raw = sub.add_parser("build-raw", help="scout candidates -> frozen raw Mix Pool")
    raw.add_argument("--input", required=True, help="scout/ranker candidate JSON")
    raw.add_argument("--date", required=True)
    raw.add_argument("--generated-at", required=True)
    raw.add_argument("--output", required=True, help="must be outside the repository")
    raw.add_argument("--source", default="daily-pipeline")
    raw.set_defaults(handler=command_build_raw)

    checker = sub.add_parser("validate-raw", help="revalidate a frozen raw Mix Pool")
    checker.add_argument("--artifact", required=True)
    checker.set_defaults(handler=command_validate_raw)

    builder = sub.add_parser("build", help="frozen raw Mix Pool -> Editorial Mix Pool")
    builder.add_argument("--raw-input", required=True,
                         help="a frozen, validated raw Mix Pool artifact (NOT scout candidates)")
    builder.add_argument("--date", required=True)
    builder.add_argument("--generated-at", required=True)
    builder.add_argument("--output", required=True, help="must be outside the repository")
    builder.add_argument("--articles", default=str(PIPELINE_DIR / "cache" / "articles"))
    builder.add_argument("--images", default=str(PIPELINE_DIR / "images.yaml"))
    builder.add_argument("--source", default="daily-pipeline")
    builder.add_argument("--no-fetch", action="store_true")
    builder.set_defaults(handler=command_build)

    publisher = sub.add_parser("publish", help="publish a built artifact to Upstash")
    publisher.add_argument("--artifact", required=True)
    publisher.add_argument("--date", required=True)
    publisher.add_argument("--timeout", type=float, default=10.0)
    publisher.set_defaults(handler=command_publish)

    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except EditorialPoolError as error:
        print(json.dumps({"status": "failed", "reason": error.reason}), file=sys.stderr)
        return EXIT_BUILD_FAILED
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "failed", "reason": type(error).__name__}), file=sys.stderr)
        return EXIT_BUILD_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
