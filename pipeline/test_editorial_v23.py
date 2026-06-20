#!/usr/bin/env python3
"""
Editorial Quality v2.3 — curly-apostrophe false-positive fix in the text-integrity gate.
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

print("1) Apostrophes / possessives / years are not flagged as unmatched quotes:")
apostrophe_terms = ["Dunn’s", "Iran’s", "Mattel’s", "X-Men ’97", "Apple’s WeatherKit"]
for term in apostrophe_terms:
    check(f"_unbalanced_issue clean for {term!r}", writer._unbalanced_issue(term) is None,
          f"got {writer._unbalanced_issue(term)!r}")
    sentence = f"{term} drew wide attention this week as officials weighed the broader fallout today."
    issues = writer.summary_quality_issues(sentence)
    check(f"  valid summary with {term!r} has no unmatched-quote issue",
          not any("unmatched" in i for i in issues), f"issues={issues}")

check("straight apostrophe \"Iran's\" clean", writer._unbalanced_issue("Iran's policy shift") is None)

print("\n2) Real quote / bracket problems still fail:")
checks_bad = {
    'curly open double  “unfinished': "This is “unfinished",
    'straight double    "unfinished': 'This is "unfinished',
    'unclosed paren     (':          "This has (unclosed bracket",
}
for label, s in checks_bad.items():
    check(f"flagged: {label}", writer._unbalanced_issue(s) is not None, f"got None for {s!r}")

check("balanced double quotes pass", writer._unbalanced_issue('She said "this is great" today.') is None)
check("balanced curly double quotes pass", writer._unbalanced_issue("He called it “a turning point” today.") is None)

print("\n3) Other quality gates unchanged:")
check("dangling 'between the U.S.' still fails",
      writer.why_quality_issues("The standoff risks widening the conflict between the U.S.", "x") != [])
check("ellipsis cut-off still fails",
      any("ellipsis" in i for i in writer.summary_quality_issues(
          "The council is still weighing the proposal and will decide after the holidays soon…")))
check("photo caption still fails",
      writer.summary_quality_issues("A worker stands near the new station holding a sign.") != [])
check("a clean summary with an apostrophe passes fully",
      writer.summary_quality_issues(
          "Apple’s WeatherKit outage left several morning apps showing stale data across the region.") == [],
      str(writer.summary_quality_issues(
          "Apple’s WeatherKit outage left several morning apps showing stale data across the region.")))

print(f"\n{'ALL PASS' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
