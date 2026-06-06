#!/usr/bin/env python3
"""
Signals — manual Publish helper (safe promotion of a validated draft to the live feed).

Promotes pipeline/generated/latest.draft.json → production latest.json, ON A BRANCH, as a
commit you then review/push/merge yourself. It is DRY-RUN by default and NEVER pushes, never
opens a PR, never merges, never deploys. The human + branch protection + the validate-feed CI
check remain the gates.

Flow this fits into:
  draft → (this script: validate + promote on publish/<date> branch + commit)
        → you push → open PR → validate-feed CI → you merge → Vercel deploys

Usage:
  python3 pipeline/publish.py            # DRY-RUN: validate + show the diff + plan. No changes.
  python3 pipeline/publish.py --apply    # create publish/<date> branch, write latest.json, commit.
                                         #   (still NO push, NO PR — prints next steps)

Hard-fails (no changes made) if: draft missing/invalid JSON, not 5 signals, date != today,
validate_feed.py rejects it, or latest.json already has uncommitted changes.
"""
import sys, os, json, shutil, argparse, datetime, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # the signals-feed repo root
DRAFT = os.path.join(HERE, "generated", "latest.draft.json")
LIVE = os.path.join(ROOT, "latest.json")
VALIDATOR = os.path.join(ROOT, "validate_feed.py")


def fail(msg):
    print(f"❌ PUBLISH ABORTED — no changes made.\n   {msg}")
    sys.exit(1)


def git(*args, check=False):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        fail(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r


def main():
    ap = argparse.ArgumentParser(description="Promote a validated draft to latest.json (manual, safe).")
    ap.add_argument("--draft", default=DRAFT)
    ap.add_argument("--apply", action="store_true",
                    help="actually create the publish branch + commit (default is dry-run)")
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    print(f"=== Signals Publish helper ({'APPLY' if args.apply else 'DRY-RUN'}) · {today} ===\n")

    # 1. draft present + valid JSON
    if not os.path.exists(args.draft):
        fail(f"draft not found: {args.draft}\n   Run the Builder first (pipeline/build.py).")
    try:
        feed = json.load(open(args.draft))
    except Exception as e:
        fail(f"draft is not valid JSON: {e}")

    # 2. explicit structural hard-fails (clear messages; validator re-checks too)
    sigs = feed.get("signals", [])
    if len(sigs) != 5:
        fail(f"expected exactly 5 signals, draft has {len(sigs)}")
    if feed.get("date") != today:
        fail(f"draft date {feed.get('date')!r} is not today ({today}) — rebuild for today before publishing")

    # 3. validate_feed.py is the authoritative gate
    print("--- validate_feed.py (against the draft) ---")
    if subprocess.run([sys.executable, VALIDATOR, args.draft]).returncode != 0:
        fail("draft failed validation (see above) — fix and rebuild; nothing promoted")
    print()

    # 4. git safety: latest.json must not already be dirty (don't clobber uncommitted work)
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        fail("not a git repository — cannot prepare a publish branch safely")
    if git("status", "--porcelain", "latest.json").stdout.strip():
        fail("production latest.json already has uncommitted changes — commit/stash them first")

    # 5. show what latest.json will become
    print("--- diff preview: latest.json → (draft) ---")
    old = json.load(open(LIVE)) if os.path.exists(LIVE) else {"date": "(none)", "signals": []}
    print(f"date : {old.get('date')}  →  {feed.get('date')}")
    old_lead = next((s for s in old.get("signals", []) if s.get("lead")), None)
    new_lead = next((s for s in sigs if s.get("lead")), None)
    print(f"lead : {old_lead['headline'][:50] if old_lead else '(none)'!r}")
    print(f"     → {new_lead['headline'][:50] if new_lead else '(none)'!r}")
    print("new five:")
    for s in sorted(sigs, key=lambda x: x.get("number", 0)):
        tag = "LEAD" if s.get("lead") else "    "
        print(f"  #{s.get('number')} {tag} imp={s.get('importance')} {s.get('category','?'):8} {s.get('headline','')[:46]}")
    stat = git("diff", "--no-index", "--stat", LIVE, args.draft).stdout.strip()
    if stat:
        print("\n" + stat)
    print()

    branch = f"publish/{today}"
    commit_msg = f"Publish Signals feed {today}"

    # 6. DRY-RUN stops here
    if not args.apply:
        print("DRY-RUN — nothing changed. To promote:")
        print(f"  python3 pipeline/publish.py --apply")
        print("which will, locally (no push):")
        print(f"  git checkout -b {branch}")
        print(f"  cp pipeline/generated/latest.draft.json latest.json")
        print(f"  git add latest.json && git commit -m \"{commit_msg}\"")
        print("\nThen YOU: push the branch, open a PR to main (validate-feed CI runs), review, and merge.")
        return

    # 7. APPLY: branch + write + commit. Never push, never PR.
    if git("rev-parse", "--verify", branch).returncode == 0:
        fail(f"branch {branch} already exists — delete it or publish from it manually")
    git("checkout", "-b", branch, check=True)
    shutil.copyfile(args.draft, LIVE)
    git("add", "latest.json", check=True)
    git("commit", "-m", commit_msg, check=True)
    head = git("rev-parse", "--short", "HEAD").stdout.strip()

    print(f"✓ promoted on branch {branch}")
    print(f"  commit {head}: {commit_msg}")
    print("  (NOT pushed, NO PR opened, production main NOT changed yet.)\n")
    print("Next (you do these manually):")
    print(f"  git push -u origin {branch}")
    print(f"  open a PR  {branch} → main   (validate-feed must go green)")
    print(f"  review the diff, then MERGE → Vercel deploys latest.json")


if __name__ == "__main__":
    main()
