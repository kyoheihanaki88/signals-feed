#!/usr/bin/env python3
"""
Signals — Publish helper (safe promotion of a validated draft to a date-keyed edition).

Promotes pipeline/generated/latest.draft.json into the date-keyed model (see ADR-0001):

    editions/<DATE>.json   (immutable edition for the morning it serves)
    latest.json            (a copy of the newest edition — pointer + fallback)

The edition DATE comes from the draft's own `date` field (set by build.py — "tomorrow UTC" by
default, or build.py --date). No timezone is pinned here; the client decides whose "today" it is.

Modes:
  python3 pipeline/publish.py            # DRY-RUN: validate + show the plan. No changes.
  python3 pipeline/publish.py --apply    # local publish/<DATE> branch: write both files + commit.
                                         #   NEVER pushes, opens a PR, merges, or deploys.
  python3 pipeline/publish.py --write     # write both files in place, NO git (used by CI, which
                                         #   then opens the PR). Runs the consistency check.

Hard-fails (no changes) if: draft missing/invalid JSON, not 5 signals, invalid date, the date
REGRESSES behind the current latest.json, or validate_feed.py rejects it.
"""
import sys, os, json, shutil, argparse, datetime, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # the signals-feed repo root
DRAFT = os.path.join(HERE, "generated", "latest.draft.json")
LIVE = os.path.join(ROOT, "latest.json")
EDITIONS = os.path.join(ROOT, "editions")
VALIDATOR = os.path.join(ROOT, "validate_feed.py")


def fail(msg):
    print(f"❌ PUBLISH ABORTED — no changes made.\n   {msg}")
    sys.exit(1)


def git(*args, check=False):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        fail(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r


def write_edition(feed, date):
    """Write editions/<date>.json + latest.json (an exact copy of it). Returns the edition path."""
    os.makedirs(EDITIONS, exist_ok=True)
    edition_path = os.path.join(EDITIONS, f"{date}.json")
    text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    with open(edition_path, "w") as f:
        f.write(text)
    with open(LIVE, "w") as f:
        f.write(text)
    return edition_path


def main():
    ap = argparse.ArgumentParser(description="Promote a validated draft to a date-keyed edition.")
    ap.add_argument("--draft", default=DRAFT)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="create the publish/<DATE> branch + commit both files (no push/PR)")
    mode.add_argument("--write", action="store_true",
                      help="write both files in place, no git (CI opens the PR)")
    args = ap.parse_args()

    label = "APPLY" if args.apply else ("WRITE" if args.write else "DRY-RUN")
    print(f"=== Signals Publish helper ({label}) ===\n")

    # 1. draft present + valid JSON
    if not os.path.exists(args.draft):
        fail(f"draft not found: {args.draft}\n   Run the Builder first (pipeline/build.py).")
    try:
        feed = json.load(open(args.draft))
    except Exception as e:
        fail(f"draft is not valid JSON: {e}")

    # 2. structural hard-fails (validator re-checks too)
    sigs = feed.get("signals", [])
    if len(sigs) != 5:
        fail(f"expected exactly 5 signals, draft has {len(sigs)}")
    try:
        feed_date = datetime.date.fromisoformat(feed.get("date"))
    except (TypeError, ValueError):
        fail(f"draft date {feed.get('date')!r} is missing or invalid (need YYYY-MM-DD)")
    date = feed_date.isoformat()

    # 3. regression guard: never publish a date OLDER than the current latest.json (same day = OK,
    #    a correction/re-publish). Timezone-free — purely date label vs date label.
    if os.path.exists(LIVE):
        try:
            cur = json.load(open(LIVE))
            cur_date = datetime.date.fromisoformat(cur.get("date"))
            if feed_date < cur_date:
                fail(f"stale-date regression: draft date {date} is older than the current "
                     f"latest.json date {cur_date.isoformat()} — rebuild for a current date")
        except (TypeError, ValueError):
            pass   # current latest.json has no parseable date; nothing to regress against

    # 4. validate_feed.py is the authoritative gate (run against the draft)
    print("--- validate_feed.py (against the draft) ---")
    if subprocess.run([sys.executable, VALIDATOR, args.draft]).returncode != 0:
        fail("draft failed validation (see above) — fix and rebuild; nothing promoted")
    print()

    edition_rel = os.path.relpath(os.path.join(EDITIONS, f"{date}.json"), ROOT)
    print(f"plan: write {edition_rel}  +  latest.json   (edition date {date})")
    old = json.load(open(LIVE)) if os.path.exists(LIVE) else {"date": "(none)", "signals": []}
    new_lead = next((s for s in sigs if s.get("lead")), None)
    print(f"  latest.json date : {old.get('date')}  →  {date}")
    print(f"  lead             : {(new_lead or {}).get('headline','(none)')[:60]!r}")

    # 5. DRY-RUN stops here
    if not args.apply and not args.write:
        print("\nDRY-RUN — nothing changed. To promote:")
        print("  python3 pipeline/publish.py --apply   (local branch + commit, no push)")
        print("  python3 pipeline/publish.py --write   (write files only; CI opens the PR)")
        return

    # 6a. APPLY: local branch + write + commit. Never push, never PR.
    if args.apply:
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            fail("not a git repository — cannot prepare a publish branch safely")
        if git("status", "--porcelain", "latest.json").stdout.strip():
            fail("latest.json already has uncommitted changes — commit/stash them first")
        branch = f"publish/{date}"
        if git("rev-parse", "--verify", branch).returncode == 0:
            fail(f"branch {branch} already exists — delete it or publish from it manually")
        git("checkout", "-b", branch, check=True)
        edition_path = write_edition(feed, date)
        run_consistency()
        git("add", os.path.relpath(edition_path, ROOT), "latest.json", check=True)
        git("commit", "-m", f"Publish Signals edition {date}", check=True)
        head = git("rev-parse", "--short", "HEAD").stdout.strip()
        print(f"\n✓ wrote {edition_rel} + latest.json, committed on {branch} ({head})")
        print("  (NOT pushed, NO PR opened, production main NOT changed yet.)")
        print("\nNext (you do these manually):")
        print(f"  git push -u origin {branch}")
        print(f"  open a PR {branch} → main  (validate-feed must go green) → review → MERGE → deploy")
        return

    # 6b. WRITE: write both files only, no git. CI creates the branch + PR.
    write_edition(feed, date)
    run_consistency()
    print(f"\n✓ wrote {edition_rel} + latest.json (no git). CI will open the PR.")


def run_consistency():
    """After writing, assert latest.json == newest edition (no stale regression)."""
    print("--- validate_feed.py --consistency ---")
    if subprocess.run([sys.executable, VALIDATOR, "--consistency", ROOT]).returncode != 0:
        fail("post-write consistency check failed (see above)")
    print()


if __name__ == "__main__":
    main()
