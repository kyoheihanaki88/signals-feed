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


def command_build(args: argparse.Namespace) -> int:
    destination = _check_destination(Path(args.output))

    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    raw_pool = mix_pool.build_mix_pool(source, args.date, args.generated_at)
    frozen_raw = mix_pool_schema.freeze_artifact(
        raw_pool, source_input=source, source=args.source
    )

    editions_dir = str(REPO_ROOT / "editions")
    # The same 90-edition cooldown the standard edition honours, plus full history so the
    # allocator can tell a cooldown expiry from a first use.
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
    _atomic_write(destination, mix_pool_schema.serialize(artifact))

    # Counts and reason codes only — never a headline, URL or summary.
    summary = {
        "status": "built",
        "date": artifact["date"],
        "candidateCount": artifact["candidateCount"],
        "minimumRequired": MINIMUM_PUBLISHABLE_POOL_SIZE,
        "failureReasons": sorted({row["reason"] for row in result.get("failures", [])}),
        "selectorPoolIdentityPrefix": str(artifact["selectorPoolIdentity"])[:12],
        "editorialPoolIdentityPrefix": str(artifact["editorialPoolIdentity"])[:12],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
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

    builder = sub.add_parser("build", help="build an Editorial Mix Pool to a non-repository path")
    builder.add_argument("--input", required=True, help="scout candidates JSON")
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
