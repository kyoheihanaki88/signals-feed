#!/usr/bin/env python3
"""
Inject `listen` (from listen_manifest.json) into a PUBLISHED edition + latest.json, in place.
Run AFTER listen_generate.py has produced + uploaded the date-scoped MP3s and written the manifest.
    python3 pipeline/listen_inject_edition.py 2026-06-30

Safety: only adds signal.listen (never touches article text / audioURL / localized.ja); writes
editions/<date>.json and (if it currently points at <date>) latest.json byte-identically; validates
the edition + repo consistency and exits non-zero (no usable change) on failure. Does NOT commit.
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
    if stats["injected"] != 5:
        sys.exit(f"❌ expected 5 listen injections, got {stats['injected']} — aborting (no write).")

    text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    open(edition, "w", encoding="utf-8").write(text)
    wrote = [os.path.relpath(edition, ROOT)]
    latest = os.path.join(ROOT, "latest.json")
    if os.path.exists(latest) and json.load(open(latest, encoding="utf-8")).get("date") == date:
        open(latest, "w", encoding="utf-8").write(text)
        wrote.append("latest.json")
    print("wrote:", ", ".join(wrote))

    v = os.path.join(ROOT, "validate_feed.py")
    rc1 = subprocess.run([sys.executable, v, edition]).returncode
    rc2 = subprocess.run([sys.executable, v, "--consistency", ROOT]).returncode
    if rc1 or rc2:
        sys.exit("❌ validation failed — do not commit.")
    print("✅ validation passed.")


if __name__ == "__main__":
    main()
