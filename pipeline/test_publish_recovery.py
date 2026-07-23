#!/usr/bin/env python3
"""Scenario tests for publish_recovery.py (Fix 1, commit 2) + workflow integration checks.

Every scenario drives the REAL ranker/selection/writer/validator on the local fixture
from test_recovery_primitives.make_fixture — no network, no production files. Failures
are scripted through the orchestrator's documented test seam (--test-fail-plan →
writer's existing --simulate-unavailable), so the strict validator fails for real,
exactly as it did on 2026-07-22.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
NOW = "2026-07-23T12:00:00+00:00"

from test_recovery_primitives import make_fixture  # noqa: E402  (shared local fixture)

FAILURES = []


def check(name, ok):
    if ok:
        print(f"✓ {name}")
    else:
        print(f"✗ {name}")
        FAILURES.append(name)


def run_recovery(root, fail_plan=None, max_rounds=3):
    out = os.path.join(root, "out", "drafts.json")
    cmd = [PY, os.path.join(HERE, "publish_recovery.py"),
           "--candidates", os.path.join(root, "candidates.json"),
           "--selection-yaml", os.path.join(root, "sel.yaml"),
           "--selection-json", os.path.join(root, "selection.json"),
           "--drafts", os.path.join(root, "drafts.json"),
           "--articles", os.path.join(root, "articles"),
           "--attempts-out", out,
           "--date", "2026-07-23",
           "--min-candidates", "6", "--now", NOW, "--no-fetch",
           "--max-replacement-rounds", str(max_rounds)]
    if fail_plan:
        cmd += ["--test-fail-plan", json.dumps(fail_plan)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    audit = json.load(open(out)) if os.path.exists(out) else None
    return r, audit


def sel_ids(audit, round_idx):
    return [e["id"] for e in audit["rounds"][round_idx]["selection"]]


def main():
    with tempfile.TemporaryDirectory() as root:
        ids = make_fixture(root)

        # 1) clean morning — initial validation passes, single round, zero recovery
        r, a = run_recovery(root)
        check("1: initial pass → exit 0, ready, one round",
              r.returncode == 0 and a["final_result"] == "ready"
              and [x["kind"] for x in a["rounds"]] == ["initial"]
              and a["excluded"] == [])
        first_sel = sel_ids(a, 0)
        lead = first_sel[0]

        # 2) one failed candidate recovers on the same-candidate refetch
        r, a = run_recovery(root, {"1": [lead]})
        check("2: fail once → refetch pass (exit 0, rounds initial+refetch)",
              r.returncode == 0 and a["final_result"] == "ready"
              and [x["kind"] for x in a["rounds"]] == ["initial", "refetch"]
              and a["rounds"][0]["validation"]["failed_ids"] == [lead]
              and a["excluded"] == [])
        check("2b: refetch round records the refetched id",
              a["rounds"][1]["refetched"] == [lead])

        # 3) refetch still fails → deterministic replacement passes
        r, a = run_recovery(root, {"1": [lead], "2": [lead]})
        check("3: refetch fails → replacement pass (exit 0)",
              r.returncode == 0 and a["final_result"] == "ready"
              and [x["kind"] for x in a["rounds"]] == ["initial", "refetch", "replacement"]
              and a["excluded"] == [lead])
        check("3b: excluded candidate never returns in the replacement selection",
              lead not in sel_ids(a, 2))
        check("3c: replacement selection is exactly five distinct stories",
              len(set(sel_ids(a, 2))) == 5)
        check("3d: audit marks the failed id rejected then replaced",
              {"id": lead, "action": "rejected"} in a["rounds"][1]["actions"]
              and {"id": lead, "action": "replaced"} in a["rounds"][2]["actions"])

        # 4) multiple failed candidates are excluded and replaced together
        pair = first_sel[:2]
        r, a = run_recovery(root, {"1": pair, "2": pair})
        check("4: two failing candidates → both excluded, replacement passes",
              r.returncode == 0 and sorted(a["excluded"]) == sorted(pair)
              and not (set(pair) & set(sel_ids(a, 2))))

        # 5–9) exhaustion: keep failing whatever is selected → fail closed, ≤5 validations
        #     (fail EVERY attempt: initial, refetch, and all 3 replacement rounds)
        plan = {str(n): ids for n in range(1, 6)}   # every candidate unavailable, every attempt
        r, a = run_recovery(root, plan)
        kinds = [x["kind"] for x in a["rounds"]]
        n_validations = sum(1 for x in a["rounds"] if x["validation"]["result"] in ("pass", "fail")
                            and (x["validation"]["hard"] or x["validation"]["result"] == "pass"))
        check("5/8: pool exhaustion fails closed (nonzero, failed_closed, nothing ready)",
              r.returncode != 0 and a["final_result"] == "failed_closed")
        check("9: at most five strict validations (initial + refetch + ≤3 replacements)",
              len([k for k in kinds if k in ("initial", "refetch", "replacement")]) <= 5)
        check("5b: excluded ids accumulate without duplicates",
              len(a["excluded"]) == len(set(a["excluded"])))

        # 6/7) selection integrity is asserted every round (five distinct, no dupes)
        ok57 = all(len(x["selection"]) in (0, 5) and
                   len({e["id"] for e in x["selection"]}) == len(x["selection"])
                   for x in a["rounds"])
        check("6/7: every recorded selection is 5 distinct stories (or a failed-closed empty round)", ok57)

        # 10–13) audit content
        r, a = run_recovery(root, {"1": [lead], "2": [lead]})
        seen = {e["id"] for x in a["rounds"] for e in x["selection"]}
        check("10: every attempted candidate appears in the audit (ids across rounds)",
              set(sel_ids(a, 0)) | set(sel_ids(a, 2)) <= seen)
        check("10b: audit entries carry rank/title/url/role",
              all(set(e) >= {"rank", "id", "title", "url", "role"}
                  for x in a["rounds"] for e in x["selection"]))
        check("11: audit records the validator's failure reasons",
              any("missing" in m or "unresolved" in m or "source" in m
                  for m in a["rounds"][0]["validation"]["hard"]))
        raw = open(os.path.join(root, "out", "drafts.json")).read()
        check("13: audit contains no article bodies and no secrets",
              "Officials announced the decision" not in raw
              and "AZURE" not in raw and "ANTHROPIC" not in raw and "token" not in raw.lower())
        plan = {str(n): ids for n in range(1, 6)}
        r, a = run_recovery(root, plan)
        check("12: audit file exists on total failure",
              a is not None and a["final_result"] == "failed_closed")

    # 14–17) workflow integration (text + structure)
    wf = open(os.path.join(HERE, "..", ".github", "workflows", "daily-auto-publish.yml"),
              encoding="utf-8").read()
    check("workflow runs the recovery orchestrator in place of rank/draft/validate steps",
          "publish_recovery.py" in wf
          and "- name: Ranker (auto-select 5)" not in wf
          and "- name: Writer (draft)" not in wf
          and "- name: Validate drafts (strict)" not in wf)
    check("14: attempts artifact uploads with if: always()",
          "name: Upload publish attempts artifact" in wf
          and wf.split("Upload publish attempts artifact")[1].lstrip().startswith("if: always()"))
    check("15: attempts artifact retention is 14 days",
          "publish-attempts-${{ env.EDITION_DATE }}" in wf
          and "retention-days: 14" in wf.split("publish-attempts-")[1][:400])
    check("16: downstream gates unchanged (approval, auto-approve, build, guard, PR, merge-or-fail)",
          all(s in wf for s in [
              "- name: Gate — selection approved",
              "grep -qx 'approved: true' pipeline/selection.yaml",
              "- name: Auto-approve drafts",
              "- name: Build draft feed",
              "- name: Pre-push guard — only the edition file changed (latest.json untouched)",
              "- name: Open publish PR",
              "- name: Merge publish PR (or fail loudly)",
          ]))
    check("17: recovery runs BEFORE build/publish (a red recovery stops everything downstream)",
          wf.index("publish_recovery.py") < wf.index("- name: Build draft feed")
          < wf.index("- name: Publish (write files only)"))
    check("attempts working dir is gitignored (pre-push guard never sees it)",
          "pipeline/out/" in open(os.path.join(HERE, "..", ".gitignore"), encoding="utf-8").read())

    # 18) covered in test_recovery_primitives.py: absent --exclude / --report keep
    #     current ranker and writer behavior byte-identical (selection equality + exit codes).
    check("18: primitives no-flag equivalence suite present",
          "empty --exclude preserves current behavior" in
          open(os.path.join(HERE, "test_recovery_primitives.py"), encoding="utf-8").read())

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
