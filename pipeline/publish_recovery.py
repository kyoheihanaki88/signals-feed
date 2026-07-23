#!/usr/bin/env python3
"""Publish recovery orchestrator (Fix 1) — rank → select → draft → strict-validate,
with automatic recovery when one extracted draft fails strict validation.

Why: the Writer is DETERMINISTIC EXTRACTIVE drafting (no LLM). When a story's source
page is thin at fetch time, its draft can fail `writer.py validate --strict` (the
2026-07-22 incident: six runs blocked by `[0ebcac] missing ['keyTakeaways']`). One bad
story must not block the whole edition when another valid candidate exists.

Control flow (max FIVE strict validations total):
  attempt 1  initial      ranker → selection.py build → writer draft → validate
  attempt 2  refetch      same selection, redraft (fresh fetch), validate
  attempts 3–5  replacement (≤3 rounds)
             ranker --exclude <cumulative failed ids> → rebuild the full five with the
             EXISTING ranking/diversity/lead/eligibility rules → draft → validate
  exhaustion → fail closed (exit 1): no partial edition, no weakened gate, nothing
             published. A selection-level failure (no per-story ids) is unrecoverable.

This script contains NO ranking or validation logic of its own — it only invokes the
existing components as subprocesses, so their gates stay authoritative and unchanged.
Every attempt is recorded in a machine-readable attempts audit (--attempts-out):
rank/id/title/url/role per candidate, validation results and reasons, and the action
taken (accepted / refetch-retry / rejected / replaced). No article bodies, no source
text, no prompts (none exist — extractive), no secrets.

Test seam: --test-fail-plan '{"1": ["id"]}' maps attempt-number → ids passed to the
Writer's EXISTING --simulate-unavailable flag for that attempt only. Local tests use it
to script failures deterministically; production never sets it.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

RANKER = os.path.join(HERE, "ranker.py")
SELECTION = os.path.join(HERE, "selection.py")
WRITER = os.path.join(HERE, "writer.py")

MAX_VALIDATIONS = 5   # initial + refetch + 3 replacement rounds


def run(cmd):
    """Run a component; stream its output (the workflow log keeps the familiar text)."""
    print(f"$ {' '.join(os.path.relpath(c) if os.path.sep in str(c) else str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, text=True).returncode


def selection_entries(selection_json):
    """[{rank,id,title,url,role}] from selection.json — audit metadata only, no bodies."""
    data = json.load(open(selection_json))
    out = []
    for s in data.get("signals", []):
        out.append({
            "rank": s.get("number"),
            "id": s.get("id"),
            "title": (s.get("title") or "")[:200],
            "url": s.get("url"),
            "role": "lead" if s.get("lead") else "supporting",
        })
    return sorted(out, key=lambda e: (e["rank"] is None, e["rank"]))


def main():
    ap = argparse.ArgumentParser(description="Daily publish rank/draft/validate with recovery.")
    ap.add_argument("--candidates", default=os.path.join(HERE, "candidates.json"))
    ap.add_argument("--selection-yaml", default=os.path.join(HERE, "selection.yaml"))
    ap.add_argument("--selection-json", default=os.path.join(HERE, "selection.json"))
    ap.add_argument("--drafts", default=os.path.join(HERE, "drafts.json"))
    ap.add_argument("--articles", default=os.path.join(HERE, "cache", "articles"))
    ap.add_argument("--attempts-out", default=os.path.join(HERE, "out", "drafts.json"))
    ap.add_argument("--date", default="", help="edition date for the audit record")
    ap.add_argument("--max-replacement-rounds", type=int, default=3)
    ap.add_argument("--min-candidates", type=int, default=None, help="forwarded to ranker (tests)")
    ap.add_argument("--now", default=None, help="forwarded to ranker (tests/determinism)")
    ap.add_argument("--summary-file", default=None, help="forwarded to ranker")
    ap.add_argument("--no-fetch", action="store_true", help="forwarded to writer (tests/offline)")
    ap.add_argument("--test-fail-plan", default="",
                    help="TEST SEAM ONLY: JSON {attempt: [ids]} → writer --simulate-unavailable")
    args = ap.parse_args()

    fail_plan = json.loads(args.test_fail_plan) if args.test_fail_plan else {}

    audit = {"date": args.date, "final_result": "failed_closed",
             "excluded": [], "final_selection": [], "rounds": []}
    excluded = []          # cumulative, ordered, no duplicates
    validations = 0

    def ranker_cmd():
        cmd = [sys.executable, RANKER, "--candidates", args.candidates, "--out", args.selection_yaml]
        if excluded:
            cmd += ["--exclude", ",".join(excluded)]
        if args.min_candidates is not None:
            cmd += ["--min-candidates", str(args.min_candidates)]
        if args.now:
            cmd += ["--now", args.now]
        if args.summary_file:
            cmd += ["--summary-file", args.summary_file]
        return cmd

    def rank_and_select():
        if run(ranker_cmd()) != 0:
            return False
        return run([sys.executable, SELECTION, "build", "--candidates", args.candidates,
                    "--selection", args.selection_yaml, "--out", args.selection_json]) == 0

    def draft(attempt):
        cmd = [sys.executable, WRITER, "draft", "--selection", args.selection_json,
               "--articles", args.articles, "--out", args.drafts]
        if args.no_fetch:
            cmd += ["--no-fetch"]
        sim = fail_plan.get(str(attempt)) or []
        if sim:
            cmd += ["--simulate-unavailable", ",".join(sim)]
        return run(cmd) == 0

    def validate():
        nonlocal validations
        validations += 1
        assert validations <= MAX_VALIDATIONS, "validation budget exceeded (bug)"
        report_path = os.path.join(os.path.dirname(args.attempts_out) or os.path.join(HERE, "out"),
                                   f"validate_report_attempt{validations}.json")
        run([sys.executable, WRITER, "validate", "--selection", args.selection_json,
             "--drafts", args.drafts, "--strict", "--report", report_path])
        return json.load(open(report_path))

    def record(attempt, kind, sel, refetched, report, actions):
        audit["rounds"].append({
            "attempt": attempt, "kind": kind, "selection": sel, "refetched": sorted(refetched),
            "validation": {"result": report.get("result", "fail") if report else "fail",
                           "hard": (report or {}).get("hard", []),
                           "warnings": (report or {}).get("warnings", []),
                           "failed_ids": (report or {}).get("failed_ids", [])},
            "actions": actions,
        })

    def finish(result, sel=None):
        audit["final_result"] = result
        audit["excluded"] = list(excluded)
        audit["final_selection"] = sel or []

    exit_code = 1
    try:
        os.makedirs(os.path.dirname(args.attempts_out) or ".", exist_ok=True)

        # ── attempt 1: initial ────────────────────────────────────────────────────────
        if not rank_and_select():
            print("::error::ranker/selection failed on the initial round — failing closed (no edition)")
            record(1, "initial", [], [], None,
                   [{"id": "(selection)", "action": "rejected"}])
            return
        sel = selection_entries(args.selection_json)
        ids = [e["id"] for e in sel]
        if len(sel) != 5 or len(set(ids)) != 5:
            print("::error::selection is not exactly 5 distinct stories — failing closed")
            record(1, "initial", sel, [], None, [{"id": "(selection)", "action": "rejected"}])
            return
        if not draft(1):
            print("::error::writer draft failed — failing closed")
            record(1, "initial", sel, [], None, [{"id": "(draft)", "action": "rejected"}])
            return
        rep = validate()
        if rep["result"] == "pass":
            record(1, "initial", sel, [], rep, [{"id": i, "action": "accepted"} for i in ids])
            finish("ready", sel)
            exit_code = 0
            return
        failed = rep.get("failed_ids", [])
        record(1, "initial", sel, [], rep,
               [{"id": i, "action": "refetch-retry"} for i in failed])
        if not failed:
            print("::error::strict validation failed at the selection level (no per-story ids) — unrecoverable, failing closed")
            return

        # ── attempt 2: same-candidate refetch (redraft; failed stories get a fresh fetch) ──
        print(f"recovery A: refetching + redrafting failed candidate(s): {', '.join(failed)}")
        if not draft(2):
            print("::error::writer draft failed during refetch — failing closed")
            record(2, "refetch", sel, failed, None, [{"id": i, "action": "rejected"} for i in failed])
            return
        rep = validate()
        if rep["result"] == "pass":
            record(2, "refetch", sel, failed, rep, [{"id": i, "action": "accepted"} for i in ids])
            finish("ready", sel)
            exit_code = 0
            return
        still = rep.get("failed_ids", [])
        record(2, "refetch", sel, failed, rep, [{"id": i, "action": "rejected"} for i in still])
        if not still:
            print("::error::strict validation failed at the selection level after refetch — unrecoverable, failing closed")
            return
        for i in still:
            if i not in excluded:
                excluded.append(i)

        # ── attempts 3..: deterministic replacement rounds ────────────────────────────
        for round_no in range(1, args.max_replacement_rounds + 1):
            attempt = 2 + round_no
            print(f"recovery B (round {round_no}/{args.max_replacement_rounds}): "
                  f"re-ranking without excluded id(s): {', '.join(excluded)}")
            replaced_now = [{"id": i, "action": "replaced"} for i in still]
            if not rank_and_select():
                print("::error::candidate pool exhausted (ranker gates failed with exclusions) — failing closed, publishing nothing")
                record(attempt, "replacement", [], [], None, replaced_now)
                return
            sel = selection_entries(args.selection_json)
            ids = [e["id"] for e in sel]
            if len(sel) != 5 or len(set(ids)) != 5 or any(i in excluded for i in ids):
                print("::error::replacement selection invalid (not 5 distinct non-excluded stories) — failing closed")
                record(attempt, "replacement", sel, [], None, replaced_now)
                return
            if not draft(attempt):
                print("::error::writer draft failed during replacement — failing closed")
                record(attempt, "replacement", sel, [], None, replaced_now)
                return
            rep = validate()
            if rep["result"] == "pass":
                record(attempt, "replacement", sel, [], rep,
                       replaced_now + [{"id": i, "action": "accepted"} for i in ids])
                finish("ready", sel)
                exit_code = 0
                return
            still = rep.get("failed_ids", [])
            record(attempt, "replacement", sel, [], rep,
                   replaced_now + [{"id": i, "action": "rejected"} for i in still])
            if not still:
                print("::error::strict validation failed at the selection level in replacement — unrecoverable, failing closed")
                return
            for i in still:
                if i not in excluded:
                    excluded.append(i)

        print(f"::error::no valid five-story set after {validations} strict validations "
              f"(excluded: {', '.join(excluded)}) — failing closed, publishing nothing")
    finally:
        # The audit is written on success AND on every failure path (fail-closed included).
        audit["excluded"] = list(excluded)
        try:
            with open(args.attempts_out, "w", encoding="utf-8") as f:
                json.dump(audit, f, ensure_ascii=False, indent=2)
            print(f"attempts audit → {os.path.relpath(args.attempts_out)}")
        except OSError as e:
            print(f"::warning::could not write attempts audit to {args.attempts_out!r}: {e}")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
