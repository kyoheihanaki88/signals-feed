"""Safe local dry-run CLI for frozen Custom Mix artifacts."""

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

from custom_mix_selector import select_custom_mix
from mix_identity import (
    POOL_SELECTOR_VERSION as SELECTOR_VERSION,   # pool ARTIFACT version (schema unchanged)
    SELECTOR_VERSION as SELECTION_VERSION,       # selection SEMANTICS version (v2 allowlist)
    normalize_regions,
    normalize_topics,
)
from mix_pool import build_mix_pool
from mix_pool_schema import (
    SCHEMA_VERSION,
    freeze_artifact,
    serialize,
    validate_artifact,
)


def _csv(values: list[str]) -> list[str]:
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_temp(path: Path) -> bool:
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(), Path("/private/tmp").resolve()}
    return any(_inside(path, root) for root in roots)


def _check_destination(path: Path, force: bool) -> None:
    resolved = path.expanduser().resolve()
    if _inside(resolved, REPO_ROOT.resolve()):
        raise ValueError(f"refusing repository/production destination: {path}")
    if resolved.exists() and not force:
        raise FileExistsError(f"destination already exists: {path}")
    if force and not _safe_temp(resolved):
        raise ValueError("--force is allowed only for an explicit temporary destination")


def _atomic_write(path: Path, payload: bytes) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _report(
    artifact: dict[str, Any],
    selection: dict[str, Any],
    regions: list[str],
    topics: list[str],
) -> dict[str, Any]:
    by_id = {row["id"]: row for row in artifact["candidates"]}
    metadata = selection["metadata"]
    logs = selection.get("candidateLogs", [])
    rejected_duplicates = sorted(
        row["id"] for row in logs if "duplicate" in str(row.get("rejectionReason", "")).lower()
    )
    return {
        "date": artifact["date"],
        "schemaVersion": artifact["schemaVersion"],
        "selectorVersion": artifact["selectorVersion"],
        "poolIdentity": artifact["poolIdentity"],
        "mixIdentity": metadata["mixIdentity"],
        "selectedRegions": regions,
        "selectedTopics": topics,
        "candidatePoolTotal": len(artifact["candidates"]),
        "qualifyingRegionCandidates": metadata.get("qualifyingRegionCandidates", 0),
        "regionalCandidatesAfterDedup": metadata.get("regionalCandidatesAfterDedup", 0),
        "selectedRegionStories": metadata.get("selectedRegionStories", 0),
        "fallbackSlots": metadata.get("fallbackSlots", 0),
        "fallbackReason": metadata.get("fallbackReason"),
        "finalRegionMix": metadata.get("finalRegionMix", {}),
        "selectedStoryIds": selection["selectedIds"],
        "selectedHeadlines": [by_id[item]["headline"] for item in selection["selectedIds"]],
        "selectionPhases": [
            {"id": row["id"], "phase": row.get("selectionPhase")}
            for row in logs
            if row.get("id") in selection["selectedIds"]
        ],
        "rejectedDuplicateIds": rejected_duplicates,
        "warnings": artifact["validation"]["warnings"],
        "validation": artifact["validation"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Offline dry run. --reference-at freezes freshness/ranking; changing only "
            "--generated-at then changes artifact metadata, not selection."
        )
    )
    result.add_argument("--input", required=True)
    result.add_argument("--date", required=True)
    result.add_argument("--generated-at", required=True)
    result.add_argument("--reference-at")
    result.add_argument("--output", required=True)
    result.add_argument("--report", required=True)
    result.add_argument("--regions", nargs="+", required=True)
    result.add_argument("--topics", nargs="*", default=[])
    result.add_argument("--story-count", type=int, default=5)
    result.add_argument("--schema-version", type=int, default=SCHEMA_VERSION)
    result.add_argument("--selector-version", type=int, default=SELECTOR_VERSION)
    result.add_argument("--force", action="store_true")
    return result


def run(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output = Path(args.output)
    report_path = Path(args.report)
    try:
        if output.resolve() == report_path.resolve():
            raise ValueError("output and report destinations must differ")
        _check_destination(output, args.force)
        _check_destination(report_path, args.force)
        if args.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        if args.selector_version != SELECTOR_VERSION:
            raise ValueError("unsupported selector version")
        source_input = json.loads(Path(args.input).read_text(encoding="utf-8"))
        reference_at = args.reference_at or args.generated_at
        pool = build_mix_pool(
            source_input,
            date=args.date,
            generated_at=args.generated_at,
            now=reference_at,
        )
        artifact = freeze_artifact(
            pool,
            source_input=source_input,
            source="offline-fixture",
            selector_version=args.selector_version,
            reference_at=reference_at,
        )
        validation = validate_artifact(artifact)
        artifact["validation"] = validation
        if not validation["valid"]:
            raise ValueError("artifact validation failed: " + "; ".join(validation["errors"]))
        regions = normalize_regions(_csv(args.regions))
        topics = normalize_topics(_csv(args.topics))
        selection = select_custom_mix(
            artifact["candidates"],
            args.date,
            regions,
            topics,
            size=args.story_count,
            # The selection identity always uses the CURRENT selection semantics version;
            # --selector-version continues to gate the pool ARTIFACT only.
            selector_version=SELECTION_VERSION,
        )
        if not selection.get("selectedIds"):
            raise ValueError("selector returned no stories")
        report = _report(artifact, selection, regions, topics)
        _atomic_write(output, serialize(artifact))
        _atomic_write(report_path, serialize(report))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"mix-pool dry run failed: {error}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
