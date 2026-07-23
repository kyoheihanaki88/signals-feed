#!/usr/bin/env python3
"""Unit tests for the two publish-recovery primitives (Fix 1, commit 1):

  • ranker.py --exclude  — deterministic candidate exclusion before selection
  • writer.py validate --strict --report — machine-readable validation reporting

Both are purely additive: absent flags must preserve current behavior exactly.
Also provides the shared local fixture builder used by test_publish_recovery.py:
a deterministic candidates.json + cached article bodies that pass the REAL ranker,
selection, writer, and strict validator with no network access.
"""
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
NOW = "2026-07-23T12:00:00+00:00"

FAILURES = []


def check(name, ok):
    if ok:
        print(f"✓ {name}")
    else:
        print(f"✗ {name}")
        FAILURES.append(name)


STORIES = [
    ("WORLD",    "Parliament approves sweeping reform of the national budget",  "parliament-budget"),
    ("BUSINESS", "Central bank raises interest rates for the third time",       "central-bank-rates"),
    ("TECH",     "Regulators publish new rules for artificial intelligence",    "ai-regulation-rules"),
    ("JAPAN",    "Tokyo unveils plan to modernize regional railways",           "tokyo-railways"),
    ("SCIENCE",  "Astronomers confirm water vapor on a distant exoplanet",      "exoplanet-water"),
    ("WORLD",    "Drought emergency declared across southern farming regions",  "drought-emergency"),
    ("BUSINESS", "Automaker reports record quarterly electric vehicle output",  "ev-output-record"),
    ("TECH",     "National grid completes rollout of smart electricity meters", "smart-meters"),
]

_BODY = """{title}.
Officials announced the decision on Wednesday after months of negotiation between the government and industry groups.
The plan will take effect within six weeks and applies to every region of the country, according to the ministry.
Supporters argued the change would strengthen public services and reduce long-term costs for households across the country.
Critics warned that the timetable was ambitious and said local authorities would need additional funding to deliver it.
Independent analysts described the announcement as the most significant shift in national policy in more than a decade.
The ministry said detailed guidance would be published next month, and a review of the program is planned within two years.
Officials also confirmed that a public consultation received more than forty thousand responses before the decision.
The measure matters because it will change how millions of people use essential services every day.
"""


def make_fixture(root):
    """Write candidates.json + articles/<id>.txt under `root`; return the ordered id list."""
    articles = os.path.join(root, "articles")
    os.makedirs(articles, exist_ok=True)
    now = datetime.datetime.fromisoformat(NOW)
    cands, ids = [], []
    for i, (cat, title, slug) in enumerate(STORIES):
        url = f"https://example.org/news/{slug}"
        cid = hashlib.sha1(url.encode()).hexdigest()[:6]
        ids.append(cid)
        cands.append({
            "id": cid, "canonical_url": url, "url": url, "title": title,
            "source": "BBC News (World)", "category": cat,
            "snippet": ("Officials said the measures announced on Wednesday would take effect within "
                        "weeks, and analysts described the move as the most significant policy shift "
                        "in years for the sector."),
            "cluster_id": f"cluster-{i}", "cluster_size": 2,
            "published_at": (now - datetime.timedelta(hours=2)).isoformat(),
            "paywalled": False,
        })
        with open(os.path.join(articles, f"{cid}.txt"), "w", encoding="utf-8") as f:
            f.write(_BODY.format(title=title))
    with open(os.path.join(root, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump({"candidates": cands}, f, indent=1)
    return ids


def ranker(root, out_name, exclude=None, min_candidates=6, extra=None):
    cmd = [PY, os.path.join(HERE, "ranker.py"),
           "--candidates", os.path.join(root, "candidates.json"),
           "--out", os.path.join(root, out_name),
           "--min-candidates", str(min_candidates), "--now", NOW]
    if exclude is not None:
        cmd += ["--exclude", exclude]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True)


def selection_ids(root, yaml_name):
    lines = open(os.path.join(root, yaml_name)).read().splitlines()
    lead = next(l.split(":", 1)[1].strip() for l in lines if l.startswith("lead:"))
    sup = [l.strip()[2:] for l in lines if l.strip().startswith("- ")]
    return [lead] + sup


def build_and_draft(root, sim_unavailable=""):
    """selection.py build + writer draft (cached articles only) → (selection.json, drafts.json)."""
    sj = os.path.join(root, "selection.json")
    dj = os.path.join(root, "drafts.json")
    r1 = subprocess.run([PY, os.path.join(HERE, "selection.py"), "build",
                         "--candidates", os.path.join(root, "candidates.json"),
                         "--selection", os.path.join(root, "sel.yaml"), "--out", sj],
                        capture_output=True, text=True)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    cmd = [PY, os.path.join(HERE, "writer.py"), "draft", "--selection", sj,
           "--articles", os.path.join(root, "articles"), "--no-fetch", "--out", dj]
    if sim_unavailable:
        cmd += ["--simulate-unavailable", sim_unavailable]
    r2 = subprocess.run(cmd, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    return sj, dj


def validate(sj, dj, report=None):
    cmd = [PY, os.path.join(HERE, "writer.py"), "validate",
           "--selection", sj, "--drafts", dj, "--strict"]
    if report:
        cmd += ["--report", report]
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    with tempfile.TemporaryDirectory() as root:
        ids = make_fixture(root)

        # ── ranker --exclude ────────────────────────────────────────────────────────
        r = ranker(root, "sel.yaml")
        check("baseline: ranker selects 5 from the fixture", r.returncode == 0)
        base = selection_ids(root, "sel.yaml")
        check("baseline: exactly 5 distinct ids", len(base) == 5 and len(set(base)) == 5)

        r = ranker(root, "sel_none.yaml", exclude="")
        check("empty --exclude preserves current behavior exactly (identical selection)",
              r.returncode == 0 and selection_ids(root, "sel_none.yaml") == base)

        one = base[0]
        r = ranker(root, "sel_ex1.yaml", exclude=one)
        ex1 = selection_ids(root, "sel_ex1.yaml")
        check("one excluded candidate: still 5 distinct, excluded id gone",
              r.returncode == 0 and len(set(ex1)) == 5 and one not in ex1)

        two = ",".join(base[:2])
        r = ranker(root, "sel_ex2.yaml", exclude=two)
        ex2 = selection_ids(root, "sel_ex2.yaml")
        check("multiple excluded candidates: still 5 distinct, none reappear",
              r.returncode == 0 and len(set(ex2)) == 5 and not (set(base[:2]) & set(ex2)))

        spare = next(i for i in ids if i not in base)
        r = ranker(root, "sel_spare.yaml", exclude=spare)
        check("excluding a NON-selected candidate leaves the selection order unchanged",
              r.returncode == 0 and selection_ids(root, "sel_spare.yaml") == base)

        r = ranker(root, "sel_det.yaml", exclude=one)
        check("deterministic across repeated runs (same exclusion → identical output)",
              r.returncode == 0 and selection_ids(root, "sel_det.yaml") == ex1)

        r = ranker(root, "sel_gate.yaml", min_candidates=9)
        check("minimum-candidate gate still fails correctly (unchanged)",
              r.returncode != 0 and "too few candidates" in r.stdout)

        r = ranker(root, "sel_pool.yaml", exclude=",".join(ids[:4]))
        check("exclusion below 5 candidates → ranker fails closed (no selection)",
              r.returncode != 0)

        # ── writer --report ─────────────────────────────────────────────────────────
        ranker(root, "sel.yaml")
        sj, dj = build_and_draft(root)
        rep = os.path.join(root, "rep_pass.json")
        r = validate(sj, dj, rep)
        d = json.load(open(rep))
        check("passing report: result=pass, no failed_ids, exit 0",
              r.returncode == 0 and d["result"] == "pass" and d["failed_ids"] == []
              and len(d["stories"]) == 5)
        check("report separates hard and warnings",
              isinstance(d["hard"], list) and isinstance(d["warnings"], list))
        r0 = validate(sj, dj)
        check("exit semantics unchanged without --report (pass → 0)", r0.returncode == 0)

        fail_one = selection_ids(root, "sel.yaml")[0]
        sj, dj = build_and_draft(root, sim_unavailable=fail_one)
        rep = os.path.join(root, "rep_fail1.json")
        r = validate(sj, dj, rep)
        d = json.load(open(rep))
        check("one failed story: result=fail, failed_ids exact, exit 1",
              r.returncode == 1 and d["result"] == "fail" and d["failed_ids"] == [fail_one])
        story = next(s for s in d["stories"] if s["id"] == fail_one)
        check("per-story hard bucket carries the reasons", len(story["hard"]) >= 1)
        check("report is written even on validation failure", os.path.exists(rep))
        check("no article bodies in the report",
              "Officials announced the decision" not in open(rep).read())
        r1 = validate(sj, dj)
        check("exit semantics unchanged without --report (fail → 1)", r1.returncode == 1)

        fail_two = ",".join(selection_ids(root, "sel.yaml")[:2])
        sj, dj = build_and_draft(root, sim_unavailable=fail_two)
        rep = os.path.join(root, "rep_fail2.json")
        validate(sj, dj, rep)
        d = json.load(open(rep))
        check("multiple failed stories: failed_ids lists each exactly once",
              sorted(d["failed_ids"]) == sorted(fail_two.split(",")))

        r = validate(sj, dj, os.path.join(os.devnull, "x", "report.json"))
        check("malformed report path fails clearly (nonzero + named error)",
              r.returncode not in (0, 1) or "cannot write validation report" in (r.stdout + r.stderr))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
