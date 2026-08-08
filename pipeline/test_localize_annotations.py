#!/usr/bin/env python3
"""ci_annotations — a silent Japanese wipe-out must be loud in CI, and never fatal.

    python3 pipeline/test_localize_annotations.py

Guards the 2026-08-07 failure mode: the Anthropic account ran out of credits, all five
signals skipped localization, and Daily Auto Publish stayed green while the edition
shipped English-only. These tests pin the four outcomes (no key / total failure /
partial failure / success) and the CI plumbing (::warning:: format, step-summary
append, exit code untouched). No network.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localize  # noqa: E402

FAILED = []


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def run(stats, *, key_present, summary_path=None):
    lines = []
    text = localize.ci_annotations(stats, key_present=key_present,
                                   summary_path=summary_path, log=lines.append)
    return text, lines


S = lambda total, localized, failed, skipped: {  # noqa: E731
    "total": total, "localized": localized, "failed": failed, "skipped": skipped}

print("outcomes")

text, lines = run(S(5, 0, 0, 5), key_present=False)
check("no key → warns", text is not None and "ANTHROPIC_API_KEY" in text)
check("no key → English-only named", "English-only" in (text or ""))

text, lines = run(S(5, 0, 5, 0), key_present=True)
check("total failure → warns in caps", text is not None and "FAILED for all 5" in text)
check("total failure → names the JA Listen consequence", "japanese_reference" in (text or ""))
check("total failure → hints at credit/key causes", "credit" in (text or ""))

text, lines = run(S(5, 3, 2, 0), key_present=True)
check("partial failure → warns with the count", text is not None and "2 of 5" in text)

text, lines = run(S(5, 5, 0, 0), key_present=True)
check("full success → silent", text is None and lines == [])

text, lines = run(S(0, 0, 0, 0), key_present=True)
check("empty feed → silent", text is None)

print("\nCI plumbing")

text, lines = run(S(5, 0, 5, 0), key_present=True)
check("stdout line is a ::warning:: workflow command",
      len(lines) == 1 and lines[0].startswith("::warning title=localize.py::"))

with tempfile.NamedTemporaryFile("r+", suffix=".md", delete=False) as tf:
    summary = tf.name
try:
    run(S(5, 0, 5, 0), key_present=True, summary_path=summary)
    body = open(summary, encoding="utf-8").read()
    check("step summary is appended", "localize.py" in body and "FAILED for all 5" in body)
    run(S(5, 5, 0, 0), key_present=True, summary_path=summary)
    check("success appends nothing", open(summary, encoding="utf-8").read() == body)
finally:
    os.unlink(summary)

text, lines = run(S(5, 0, 5, 0), key_present=True, summary_path="/nonexistent/dir/summary.md")
check("unwritable summary degrades to a warning, not a crash",
      any("could not append" in l for l in lines))

print("\nend to end — exit code stays 0 (best-effort is a shipping guarantee)")

with tempfile.TemporaryDirectory() as td:
    feed_path = os.path.join(td, "feed.json")
    json.dump({"date": "2026-01-01",
               "signals": [{"number": 1, "headline": "H", "summary": "S",
                            "keyTakeaways": ["K"], "whyItMatters": "W"}]},
              open(feed_path, "w"))
    env = {**os.environ, "GITHUB_STEP_SUMMARY": os.path.join(td, "summary.md")}
    env.pop("ANTHROPIC_API_KEY", None)     # forces the all-skipped path, no network
    r = subprocess.run([sys.executable, str(Path(localize.__file__)), feed_path],
                       capture_output=True, text=True, env=env)
    check("exit code is 0 with zero localized", r.returncode == 0)
    check("::warning:: reaches real stdout", "::warning title=localize.py::" in r.stdout)
    check("real $GITHUB_STEP_SUMMARY file is written",
          os.path.exists(env["GITHUB_STEP_SUMMARY"])
          and "localize.py" in open(env["GITHUB_STEP_SUMMARY"], encoding="utf-8").read())

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
