#!/usr/bin/env python3
"""
Regression tests for the Daily Auto Publish summary failures (Increment F follow-up):
  - "summary too short (<12 words)"
  - "summary begins mid-sentence (not capitalized)"

Proves the Writer now either REPAIRS the summary (so it passes the SAME strict checks the validator
runs) or FAILS CLOSED by flagging the draft — it never emits a passing-but-malformed summary.

Run: python3 pipeline/test_writer_summary.py   (stdlib only; exits non-zero on any failure)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import writer  # noqa: E402

PASS, FAIL = "✓", "✗"
failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures += 1


def item(title, _id="t0001"):
    return {"id": _id, "number": 1, "lead": True, "category": "BUSINESS",
            "source": "Reuters", "url": "https://example.com/news/story", "title": title}


# ── Case A: a too-SHORT headline-relevant sentence must be repaired (joined to reach >=12 words) ──
print("Case A — 'summary too short (<12 words)' is repaired by joining sentences:")
src_a = ("Funding is changing fast this year. Transit funding now shifts to private capital. "
         "Investors cover the upfront costs of lighting, rail upkeep, and parks in exchange for fees "
         "over several decades. Critics warn it cedes public control.")
d_a = writer.draft_one(item("Transit funding shifts to private capital"), src_a, "full_article")
sum_a = d_a["draft"]["summary"]
issues_a = writer.summary_quality_issues(sum_a)
check("summary passes strict quality checks", issues_a == [], f"issues={issues_a} :: {sum_a!r}")
check("summary has >=12 words", len(sum_a.split()) >= 12, f"{len(sum_a.split())} words")
check("not flagged needs_review (it was repairable)", "needs_review" not in d_a["flags"], str(d_a["flags"]))


# ── Case B: a MID-SENTENCE (lowercase-start) fragment must not be chosen ──
print("\nCase B — 'summary begins mid-sentence (not capitalized)' is avoided:")
src_b = ("policy shifts as cities adapt to new funding pressures. The council approved a long-term "
         "concession with private investors covering upfront transit and lighting costs for decades. "
         "Officials said the arrangement avoids an immediate tax increase.")
d_b = writer.draft_one(item("Cities fund transit with private capital"), src_b, "full_article")
sum_b = d_b["draft"]["summary"]
check("summary starts with a capital letter", sum_b[:1].isupper(), f"starts {sum_b[:1]!r} :: {sum_b!r}")
check("summary passes strict quality checks", writer.summary_quality_issues(sum_b) == [],
      f"{writer.summary_quality_issues(sum_b)} :: {sum_b!r}")


# ── Case C: prose that CANNOT form a quality summary fails closed (flagged), never ships a fragment ─
print("\nCase C — unsummarizable prose fails closed (flagged), no bad summary emitted:")
src_c = ("shifts in funding occurred over several years. concessions handed control to investors "
         "quietly over time.")  # every sentence begins mid-clause → no capitalized start to anchor
d_c = writer.draft_one(item("A funding story"), src_c, "rss_snippet")
sum_c = d_c["draft"]["summary"]
check("no fragment emitted (empty summary)", sum_c == "", f"got {sum_c!r}")
check("flagged needs_review (fail-closed)", "needs_review" in d_c["flags"], str(d_c["flags"]))


# ── Invariant: a draft that is NOT flagged must always carry a strict-valid summary ──
print("\nInvariant — unflagged draft ⇒ strict-valid summary (no bad summary can pass):")
for label, d in (("A", d_a), ("B", d_b), ("C", d_c)):
    flagged = any(f in d["flags"] for f in
                  ("needs_review", "source_unavailable", "thin_source",
                   "summary_needs_human", "whyItMatters_needs_human"))
    s = d["draft"]["summary"]
    ok = flagged or writer.summary_quality_issues(s) == []
    check(f"draft {label}: flagged-or-valid", ok, f"flags={d['flags']} summary={s!r}")


print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
