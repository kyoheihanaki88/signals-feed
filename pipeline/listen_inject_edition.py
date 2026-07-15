#!/usr/bin/env python3
"""
Inject `listen` (from listen_manifest.json) into a PUBLISHED edition + latest.json, in place.
Run AFTER listen_generate.py has produced + uploaded the date-scoped MP3s and written the manifest.
    python3 pipeline/listen_inject_edition.py 2026-06-30

Safety: only adds signal.listen (never touches article text / audioURL / localized.ja); writes
editions/<date>.json and PROMOTES latest.json to it byte-identically (only forward — never moves the
served pointer to an older date). This is the sole place latest.json advances to a new date, so the
served pointer is never audio-less. Validates the edition + repo consistency and exits non-zero (no
usable change) on failure. Does NOT commit.
"""
import sys, os, json, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from listen import inject_listen, load_listen_manifest  # noqa: E402

ROOT = os.path.dirname(HERE)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python3 pipeline/listen_inject_edition.py <YYYY-MM-DD>")
    date = sys.argv[1]
    edition = os.path.join(ROOT, "editions", f"{date}.json")
    if not os.path.exists(edition):
        sys.exit(f"❌ {edition} not found")
    feed = json.load(open(edition, encoding="utf-8"))
    if feed.get("date") != date:
        sys.exit(f"❌ {edition} internal date {feed.get('date')!r} != {date}")

    feed, stats = inject_listen(feed, date, load_listen_manifest())
    print("inject stats:", stats)
    # All 5 signals must end listen-present (freshly injected, merged, or already-preserved) and this
    # run must have CHANGED something (a fresh EN inject → injected==5; a later JA pass → merged==5).
    # A no-op run (nothing new) aborts without writing. EN's fail-closed guarantee is still enforced
    # downstream by validate_feed (listen_ready = listen.en for all 5) after the write.
    covered = stats["injected"] + stats["merged"] + stats["preserved"]
    changed = stats["injected"] + stats["merged"]
    if covered != 5 or changed == 0:
        sys.exit(f"❌ expected all 5 signals listen-present with a change this run "
                 f"(injected+merged+preserved==5 and injected+merged>0); got {stats} — aborting (no write).")

    text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    open(edition, "w", encoding="utf-8").write(text)
    wrote = [os.path.relpath(edition, ROOT)]

    # PROMOTE latest.json to this now-Listen-complete edition. This is the ONLY place the served
    # pointer advances to a new date (Daily Auto Publish deliberately leaves latest.json alone —
    # fail-closed). Write latest.json only when this date is >= the current latest date, so the
    # pointer is never moved BACKWARDS. Because `text` already contains the injected EN audio, the
    # promoted latest.json is never audio-less.
    latest = os.path.join(ROOT, "latest.json")
    cur = json.load(open(latest, encoding="utf-8")).get("date") if os.path.exists(latest) else None
    if cur is None or date >= cur:
        open(latest, "w", encoding="utf-8").write(text)
        wrote.append("latest.json")
    else:
        print(f"latest.json stays at {cur} (newer than {date}) — not moving the pointer backwards")
    print("wrote:", ", ".join(wrote))

    v = os.path.join(ROOT, "validate_feed.py")
    rc1 = subprocess.run([sys.executable, v, edition]).returncode
    rc2 = subprocess.run([sys.executable, v, "--consistency", ROOT]).returncode
    if rc1 or rc2:
        sys.exit("❌ validation failed — do not commit.")
    print("✅ validation passed.")


if __name__ == "__main__":
    main()
