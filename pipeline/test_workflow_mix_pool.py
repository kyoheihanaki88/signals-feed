#!/usr/bin/env python3
"""Phase 3D-3D — the daily workflow's Custom Mix publication block.

Structural assertions against the real workflow file. Nothing here executes a workflow,
contacts GitHub or publishes anything: the point is to prove the standard edition is
untouched, the Custom Mix steps are strictly additive and last, credentials travel only
through Actions secrets, and no generated pool can reach the repository.
"""

import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW = os.path.join(REPO, ".github", "workflows", "daily-auto-publish.yml")

FAILURES = []


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


RAW = open(WORKFLOW, encoding="utf-8").read()
DOC = yaml.safe_load(RAW)
STEPS = DOC["jobs"]["auto-publish"]["steps"]
NAMES = [str(step.get("name", "")) for step in STEPS]


def index_of(fragment):
    for position, name in enumerate(NAMES):
        if fragment.lower() in name.lower():
            return position
    return -1


# ── 45. the file still parses ─────────────────────────────────────────────────────────

check("45. the workflow is valid YAML with both jobs intact",
      isinstance(DOC, dict) and set(DOC["jobs"]) == {"probe", "auto-publish"},
      str(list(DOC.get("jobs", {}))))

# ── 38. the standard edition is present and correctly ordered ─────────────────────────

STANDARD = [
    "Checkout main",
    "Set up Python",
    "Resolve edition date",
    "Gate — target is not more than 1 day ahead",
    "Gate — target edition does not already exist",
    "Gate — target is newer than current latest.json",
    "Scout (live RSS)",
    "Rank, draft, validate",
    "Gate — selection approved",
    "Build draft feed",
    "Gate — draft date matches target",
    "Publish (write files only)",
    "Pre-push guard",
    "Open publish PR",
    "Merge publish PR",
]
positions = [index_of(fragment) for fragment in STANDARD]
check("38. every standard edition step is still present",
      all(position >= 0 for position in positions),
      str([STANDARD[i] for i, p in enumerate(positions) if p < 0]))
check("38b. the standard edition steps are still in their original order",
      positions == sorted(positions), str(positions))

# ── 39-40. the Custom Mix steps are additive, last, and gated ─────────────────────────

MIX_GATE = index_of("Custom Mix — are the Upstash publisher secrets provisioned?")
MIX_BUILD = index_of("Custom Mix — build the Editorial Mix Pool")
MIX_PUBLISH = index_of("Custom Mix — publish the Editorial Mix Pool")
MIX_ASSERT = index_of("Custom Mix — assert nothing was added")

check("39. the Custom Mix steps exist",
      min(MIX_GATE, MIX_BUILD, MIX_PUBLISH, MIX_ASSERT) >= 0,
      str([MIX_GATE, MIX_BUILD, MIX_PUBLISH, MIX_ASSERT]))
check("39b. they are purely additive — they come AFTER every standard edition step",
      MIX_GATE > max(positions), f"{MIX_GATE} vs {max(positions)}")
check("39c. a Custom Mix failure cannot roll back the edition: it runs after the merge",
      MIX_BUILD > index_of("Merge publish PR"))
check("40. publication happens only after the build step",
      MIX_GATE < MIX_BUILD < MIX_PUBLISH)

mix_steps = [STEPS[i] for i in (MIX_BUILD, MIX_PUBLISH)]
check("40b. both build and publish are gated on the secrets being provisioned",
      all("steps.mixsecrets.outputs.configured == 'true'" in str(step.get("if", ""))
          for step in mix_steps))
check("40c. the build fails closed — no continue-on-error anywhere in the block",
      not any(step.get("continue-on-error") for step in
              [STEPS[i] for i in (MIX_GATE, MIX_BUILD, MIX_PUBLISH, MIX_ASSERT)]))
check("40d. the publish step runs the CLI's `publish` subcommand, never a default action",
      "editorial_mix_pool_cli.py publish" in str(STEPS[MIX_PUBLISH]["run"]))
check("40e. the build step revalidates by building, not by trusting a previous artifact",
      "editorial_mix_pool_cli.py build" in str(STEPS[MIX_BUILD]["run"]))

# ── 41-42. secrets ────────────────────────────────────────────────────────────────────

secret_refs = set(re.findall(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}", RAW))
check("41. the Upstash credentials are referenced only as GitHub Actions secrets",
      {"KV_REST_API_URL", "KV_REST_API_WRITE_TOKEN"} <= secret_refs, str(sorted(secret_refs)))

mix_block = "\n".join(
    yaml.safe_dump(STEPS[i], sort_keys=False) for i in (MIX_GATE, MIX_BUILD, MIX_PUBLISH, MIX_ASSERT)
)
check("41b. the credentials reach the process through `env:` only",
      "KV_REST_API_WRITE_TOKEN" in str(STEPS[MIX_PUBLISH].get("env", {}))
      and "KV_REST_API_WRITE_TOKEN" not in str(STEPS[MIX_PUBLISH]["run"]))
check("42. no secret is echoed, printed or passed on a command line",
      not re.search(r"echo[^\n]*\$\{?KV_REST", mix_block)
      and not re.search(r"--token|--url[ =]", mix_block)
      and "$KV_REST_API_WRITE_TOKEN" not in str(STEPS[MIX_PUBLISH]["run"]))
check("42b. the gate step reports presence as a boolean, never a value",
      "configured=true" in str(STEPS[MIX_GATE]["run"])
      and "echo \"$KV_REST" not in str(STEPS[MIX_GATE]["run"]))
check("42c. no real credential value appears anywhere in the workflow",
      not re.search(r"upstash\.io", RAW.replace("UPSTASH_PROVISIONING.md", "")))

# ── 43-44. the generated pool never touches the repository ────────────────────────────

check("44. the build writes to the runner temp directory, not a repository path",
      "$RUNNER_TEMP/editorial-mix-pool-" in str(STEPS[MIX_BUILD]["run"]))
check("44b. no Custom Mix step writes into the repository tree",
      not re.search(r"--output\s+(?!\"?\$RUNNER_TEMP)", str(STEPS[MIX_BUILD]["run"])))
check("43. an explicit step asserts no generated pool entered the repository",
      "editorial-mix-pool-*.json" in str(STEPS[MIX_ASSERT]["run"])
      and "::error::" in str(STEPS[MIX_ASSERT]["run"]))
check("43b. that step also asserts latest.json and editions/ were not disturbed",
      "latest.json editions/" in str(STEPS[MIX_ASSERT]["run"]))
check("43c. no Custom Mix step commits, pushes or opens a PR",
      not re.search(r"git (add|commit|push)|gh pr (create|merge)", mix_block))

# The CLI itself refuses a repository destination, so the workflow is the second line of
# defence rather than the only one.
cli = open(os.path.join(HERE, "editorial_mix_pool_cli.py"), encoding="utf-8").read()
check("44c. the CLI refuses a repository destination independently of the workflow",
      "refusing a repository destination" in cli and "_check_destination" in cli)

# ── 46. documented pre-production behaviour when secrets are absent ───────────────────

gate_run = str(STEPS[MIX_GATE]["run"])
check("46. absent secrets produce an explicit, visible skip rather than a silent pass",
      "configured=false" in gate_run and "::notice::" in gate_run
      and "GITHUB_STEP_SUMMARY" in gate_run)
check("46b. the skip notice points at the provisioning document",
      "UPSTASH_PROVISIONING.md" in gate_run)
check("46c. a skipped Custom Mix never reports the pool as available",
      "published" not in gate_run.lower())
check("46d. the documented behaviour is written down, not only implemented",
      os.path.exists(os.path.join(REPO, "UPSTASH_PROVISIONING.md")))

# ── 47. nothing here can publish for real ─────────────────────────────────────────────

check("47. no workflow is enabled to publish a real artifact during tests",
      not re.search(r"upstash_smoke_test", RAW))
# Parse this file rather than grepping it — the grep would match the names written here.
import ast  # noqa: E402

imported = set()
for node in ast.walk(ast.parse(open(__file__, encoding="utf-8").read())):
    if isinstance(node, ast.Import):
        imported.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        imported.add(node.module.split(".")[0])
forbidden = imported & {"subprocess", "socket", "urllib", "http", "requests",
                        "upstash_mix_pool_transport", "upstash_smoke_test"}
check("47b. this test executes no workflow step and opens no socket",
      forbidden == set(), str(sorted(forbidden)))

# ── the other workflows are untouched ─────────────────────────────────────────────────

others = [
    name for name in sorted(os.listdir(os.path.join(REPO, ".github", "workflows")))
    if name != "daily-auto-publish.yml"
]
polluted = [
    name for name in others
    if "editorial_mix_pool" in open(
        os.path.join(REPO, ".github", "workflows", name), encoding="utf-8").read()
]
check("no other workflow was modified to publish a pool", polluted == [], str(polluted))

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}:")
    for name in FAILURES:
        print("  -", name)
    sys.exit(1)
print("All workflow checks passed.")
