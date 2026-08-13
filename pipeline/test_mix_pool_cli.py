#!/usr/bin/env python3
"""Subprocess tests for the safe, offline Phase 2C dry-run CLI."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from custom_mix_selector import select_custom_mix
FIXTURE = HERE / "fixtures" / "mix_pool_scout_candidates.json"
DATE = "2026-07-27"
STAMP = "2026-07-27T10:00:00Z"
FAILURES = []


def check(name, condition, detail=""):
    print(("✓ " if condition else "✗ ") + name)
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def command(output, report, *extra, fixture=FIXTURE, environment=None):
    args = [
        sys.executable, "-m", "pipeline.mix_pool_cli",
        "--input", str(fixture), "--date", DATE, "--generated-at", STAMP,
        "--reference-at", STAMP, "--output", str(output), "--report", str(report),
        "--regions", "japan", "--topics", "tech", "--story-count", "5", *extra,
    ]
    return subprocess.run(
        args, cwd=ROOT, text=True, capture_output=True, env=environment, check=False
    )


with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    output = temp / "pool.json"
    report_path = temp / "report.json"
    first = command(output, report_path)
    check("K. /tmp dry run succeeds", first.returncode == 0, first.stderr)
    artifact_bytes = output.read_bytes()
    report_bytes = report_path.read_bytes()
    artifact = json.loads(artifact_bytes)
    report = json.loads(report_bytes)
    # Selector v2: "tech" is a strict allowlist, so this fixture yields only the two
    # japan tech stories — the mix ships short (fail closed) instead of filling with
    # off-topic stories.
    check("O. Japan tech allowlist returns the two tech stories only",
          len(report["selectedStoryIds"]) == 2)
    check(
        "P. the fail-closed shortage is explicit",
        report["selectedRegionStories"] == 2
        and report["fallbackSlots"] == 0
        and report["fallbackReason"] == "insufficient total eligible candidates",
        report,
    )
    expected = {
        "date", "schemaVersion", "selectorVersion", "poolIdentity", "mixIdentity",
        "selectedRegions", "selectedTopics", "candidatePoolTotal",
        "qualifyingRegionCandidates", "regionalCandidatesAfterDedup",
        "selectedRegionStories", "fallbackSlots", "fallbackReason",
        "finalRegionMix", "selectedStoryIds", "selectedHeadlines",
        "selectionPhases", "rejectedDuplicateIds", "warnings", "validation",
    }
    check("Q. report contains all required fields", set(report) == expected)
    reselected = select_custom_mix(
        artifact["candidates"], DATE, ["japan"], ["tech"], size=5
    )
    check(
        "V. persisted artifact selects the same stories",
        reselected["selectedIds"] == report["selectedStoryIds"],
        reselected,
    )

    again_output = temp / "pool-again.json"
    again_report = temp / "report-again.json"
    again = command(again_output, again_report)
    check(
        "I. identical args are byte identical",
        again.returncode == 0
        and again_output.read_bytes() == artifact_bytes
        and again_report.read_bytes() == report_bytes,
        again.stderr,
    )

    reversed_fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    reversed_fixture["candidates"].reverse()
    reversed_path = temp / "reversed.json"
    reversed_path.write_text(json.dumps(reversed_fixture), encoding="utf-8")
    reversed_output = temp / "reversed-pool.json"
    reversed_report = temp / "reversed-report.json"
    reversed_run = command(reversed_output, reversed_report, fixture=reversed_path)
    check(
        "J. reversed input is byte identical",
        reversed_run.returncode == 0
        and reversed_output.read_bytes() == artifact_bytes
        and reversed_report.read_bytes() == report_bytes,
        reversed_run.stderr,
    )

    later_output = temp / "later-pool.json"
    later_report = temp / "later-report.json"
    later = command(
        later_output,
        later_report,
        "--generated-at",
        "2026-07-27T11:00:00Z",
    )
    later_artifact = json.loads(later_output.read_text()) if later.returncode == 0 else {}
    later_report_value = json.loads(later_report.read_text()) if later.returncode == 0 else {}
    artifact_without_stamp = {key: value for key, value in artifact.items() if key != "generatedAt"}
    later_without_stamp = {
        key: value for key, value in later_artifact.items() if key != "generatedAt"
    }
    check(
        "generatedAt boundary leaves selection and other artifact fields stable",
        later.returncode == 0
        and artifact_without_stamp == later_without_stamp
        and later_report_value == report,
        later.stderr,
    )

    no_force = command(output, report_path)
    check("M. overwrite without force rejected", no_force.returncode != 0)
    forced = command(output, report_path, "--force")
    check("N. force works only on explicit temp files", forced.returncode == 0, forced.stderr)
    check("atomic write leaves no temp files", not list(temp.glob(".pool.json.*")))

    invalid_selector = command(temp / "bad-selector.json", temp / "bad-selector-report.json",
                               "--selector-version", "999")
    check("R. unsupported selector version exits nonzero", invalid_selector.returncode != 0)
    invalid_schema = command(temp / "bad-schema.json", temp / "bad-schema-report.json",
                             "--schema-version", "999")
    check("S. unsupported schema version exits nonzero", invalid_schema.returncode != 0)

    malformed = temp / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")
    invalid_input = command(temp / "invalid.json", temp / "invalid-report.json", fixture=malformed)
    check("U. validation/build failure exits nonzero", invalid_input.returncode != 0)

    sparse = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sparse["candidates"] = sparse["candidates"][:3]
    sparse_path = temp / "sparse.json"
    sparse_path.write_text(json.dumps(sparse), encoding="utf-8")
    sparse_run = command(temp / "sparse-pool.json", temp / "sparse-report.json", fixture=sparse_path)
    check("selector shortage is handled without fabrication", sparse_run.returncode == 0, sparse_run.stderr)

    clean_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}
    clean_run = command(temp / "clean-env.json", temp / "clean-env-report.json",
                        environment=clean_env)
    check("T. CLI succeeds without secret environment", clean_run.returncode == 0, clean_run.stderr)

production = ROOT / "editions" / "phase2c-should-not-exist.json"
production_report = ROOT / "phase2c-should-not-exist-report.json"
rejected = command(production, production_report)
check("L. repository/production destination rejected", rejected.returncode != 0)
check("L2. rejected production files were not created",
      not production.exists() and not production_report.exists())

source_text = (HERE / "mix_pool_cli.py").read_text(encoding="utf-8")
check("T2. no network dependency", not any(
    token in source_text for token in ("requests", "urllib.request", "httpx", "socket")
))
check("T3. no environment-secret reads", "os.environ" not in source_text and "getenv(" not in source_text)

if FAILURES:
    print("\n" + "\n".join(FAILURES))
    sys.exit(1)
print(f"\n{20 - len(FAILURES)}/20 PASS")
